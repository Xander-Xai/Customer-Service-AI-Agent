#!/usr/bin/env python3
"""Distributed Runtime Foundation 可复现验证脚本。

对真实基础设施运行 4 项检查并输出机器可读 artifact：

  1. checkpoint_cross_process  —— 实例 A 写 checkpoint，实例 B 读同一 thread
  2. same_thread_serialization —— 同 thread 跨 client 互斥
  3. different_thread_parallelism —— 不同 thread 可并发
  4. tool_idempotency          —— 重试不重复副作用

基础设施（未配置则对应检查 NOT_RUN，脚本仍退出 0）：

  DISTRIBUTED_DB_URL / TEST_POSTGRES_CHECKPOINT_URL / DATABASE_URL  (PostgreSQL)
  TEST_REDIS_URL / REDIS_URL                                        (Redis)

输出：artifacts/distributed-runtime/<UTC 时间戳>/report.json
用法：
  python scripts/verify_distributed_runtime.py
  DISTRIBUTED_DB_URL=postgresql://... TEST_REDIS_URL=redis://... python scripts/verify_distributed_runtime.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "distributed-runtime-evidence/v1"


def _git_sha() -> str:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO_ROOT,
            text=True,
        ).strip()
        return f"{sha}+dirty" if dirty else sha
    except Exception:
        return "unknown"


def _first_env(*names: str) -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""


def _is_postgres(url: str) -> bool:
    return url.startswith(("postgresql://", "postgres://"))


def _tiny_graph(checkpointer):
    from langgraph.graph import StateGraph

    class S(TypedDict, total=False):
        value: int

    g = StateGraph(S)
    g.add_node("inc", lambda s: {"value": s.get("value", 0) + 1})
    g.set_entry_point("inc")
    g.set_finish_point("inc")
    return g.compile(checkpointer=checkpointer)


async def check_checkpoint_cross_process(pg_url: str) -> dict[str, Any]:
    from core.checkpointer import build_postgres_checkpointer, close_checkpoint_runtime

    thread = f"verify-cp-{uuid.uuid4().hex}"
    config = {"configurable": {"thread_id": thread}}
    rt_a = await build_postgres_checkpointer(pg_url, setup_timeout=10.0)
    rt_b = await build_postgres_checkpointer(pg_url, setup_timeout=10.0)
    try:
        graph_a = _tiny_graph(rt_a.checkpointer)
        graph_b = _tiny_graph(rt_b.checkpointer)
        await graph_a.ainvoke({"value": 41}, config=config)
        state_b = await graph_b.aget_state(config)
        ok = state_b.values.get("value") == 42
        await rt_a.checkpointer.adelete_thread(thread)
        return {"status": "PASS" if ok else "FAIL", "thread_id": thread}
    finally:
        await close_checkpoint_runtime(rt_a)
        await close_checkpoint_runtime(rt_b)


async def check_same_thread_serialization(redis_url: str) -> dict[str, Any]:
    import redis.asyncio as aioredis

    from runtime.thread_lock import RedisThreadLock

    ca = aioredis.from_url(redis_url, decode_responses=True)
    cb = aioredis.from_url(redis_url, decode_responses=True)
    la, lb = RedisThreadLock(ca), RedisThreadLock(cb)
    thread = f"verify-serial-{uuid.uuid4().hex}"
    try:
        got_a = await la.acquire(thread, "a", 30)
        got_b = await lb.acquire(thread, "b", 30)  # 应为 False
        await la.release(thread, "a")
        got_b_after = await lb.acquire(thread, "b", 30)  # 释放后应为 True
        await lb.release(thread, "b")
        ok = got_a and not got_b and got_b_after
        return {
            "status": "PASS" if ok else "FAIL",
            "acquired_first": got_a,
            "second_acquired_while_held": got_b,
            "second_acquired_after_release": got_b_after,
        }
    finally:
        await ca.aclose()
        await cb.aclose()


async def check_different_thread_parallelism(redis_url: str) -> dict[str, Any]:
    import redis.asyncio as aioredis

    from runtime.thread_lock import RedisThreadLock

    ca = aioredis.from_url(redis_url, decode_responses=True)
    cb = aioredis.from_url(redis_url, decode_responses=True)
    la, lb = RedisThreadLock(ca), RedisThreadLock(cb)
    t1, t2 = f"verify-par-{uuid.uuid4().hex}-1", f"verify-par-{uuid.uuid4().hex}-2"
    try:
        a = await la.acquire(t1, "a", 30)
        b = await lb.acquire(t2, "b", 30)
        await la.release(t1, "a")
        await lb.release(t2, "b")
        return {"status": "PASS" if (a and b) else "FAIL", "t1": a, "t2": b}
    finally:
        await ca.aclose()
        await cb.aclose()


async def check_tool_idempotency() -> dict[str, Any]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from db.models import Base
    from runtime.side_effects import SideEffectStore, execute_idempotent_operation

    fd, path = tempfile.mkstemp(suffix=".db", prefix="verify-idem-")
    os.close(fd)
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    store = SideEffectStore(session_factory=sessionmaker(bind=engine))
    calls = {"n": 0}

    def side_effect():
        calls["n"] += 1
        return {"refund_id": "R-1"}

    try:
        await execute_idempotent_operation(
            tool_name="refund",
            operation_key="refund:order-1:req-1",
            run_id="run-1",
            thread_id="T",
            arguments={"order_id": "order-1"},
            operation=side_effect,
            store=store,
        )
        second = await execute_idempotent_operation(
            tool_name="refund",
            operation_key="refund:order-1:req-1",
            run_id="run-2",  # 重试产生新 run，同一业务操作键
            thread_id="T",
            arguments={"order_id": "order-1"},
            operation=side_effect,
            store=store,
        )
        ok = calls["n"] == 1 and second == {"refund_id": "R-1"}
        return {"status": "PASS" if ok else "FAIL", "side_effect_calls": calls["n"]}
    finally:
        engine.dispose()
        with __import__("contextlib").suppress(Exception):
            os.remove(path)


async def _run() -> dict[str, Any]:
    # 让 process env 覆盖 .env，并避免生产校验在 import 时触发
    import dotenv
    from dotenv import dotenv_values

    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for key, value in dotenv_values(env_path).items():
            if value is not None:
                os.environ.setdefault(key, value)
    dotenv.load_dotenv = lambda *a, **k: False  # type: ignore[assignment]
    os.environ.setdefault("DEV_MODE", "true")
    os.environ.setdefault("API_KEY_ENABLED", "false")
    sys.path.insert(0, str(REPO_ROOT))

    pg_url = _first_env("DISTRIBUTED_DB_URL", "TEST_POSTGRES_CHECKPOINT_URL", "DATABASE_URL")
    redis_url = _first_env("TEST_REDIS_URL", "REDIS_URL")

    checks: dict[str, Any] = {}

    if _is_postgres(pg_url):
        try:
            checks["checkpoint_cross_process"] = await check_checkpoint_cross_process(pg_url)
        except Exception as e:
            checks["checkpoint_cross_process"] = {
                "status": "FAIL",
                "error": f"{type(e).__name__}: {e}",
            }
    else:
        checks["checkpoint_cross_process"] = {"status": "NOT_RUN", "reason": "no PostgreSQL URL"}

    if redis_url:
        try:
            checks["same_thread_serialization"] = await check_same_thread_serialization(redis_url)
        except Exception as e:
            checks["same_thread_serialization"] = {
                "status": "FAIL",
                "error": f"{type(e).__name__}: {e}",
            }
        try:
            checks["different_thread_parallelism"] = await check_different_thread_parallelism(
                redis_url
            )
        except Exception as e:
            checks["different_thread_parallelism"] = {
                "status": "FAIL",
                "error": f"{type(e).__name__}: {e}",
            }
    else:
        checks["same_thread_serialization"] = {"status": "NOT_RUN", "reason": "no Redis URL"}
        checks["different_thread_parallelism"] = {"status": "NOT_RUN", "reason": "no Redis URL"}

    try:
        checks["tool_idempotency"] = await check_tool_idempotency()
    except Exception as e:
        checks["tool_idempotency"] = {"status": "FAIL", "error": f"{type(e).__name__}: {e}"}

    statuses = {name: c.get("status") for name, c in checks.items()}
    overall = (
        "PASS"
        if statuses and all(s == "PASS" for s in statuses.values())
        else ("NOT_RUN" if all(s == "NOT_RUN" for s in statuses.values()) else "FAIL")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "overall_status": overall,
        "checks": checks,
    }


def main() -> int:
    report = asyncio.run(_run())
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = REPO_ROOT / "artifacts" / "distributed-runtime" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {"overall_status": report["overall_status"], "output": str(out_path)},
            ensure_ascii=False,
        )
    )
    # 未配置基础设施（NOT_RUN）不算失败；配置了但检查失败才返回 1
    return 0 if report["overall_status"] in ("PASS", "NOT_RUN") else 1


if __name__ == "__main__":
    raise SystemExit(main())
