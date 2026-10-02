#!/usr/bin/env python3
"""Distributed Runtime Foundation 可复现验证脚本。

对真实基础设施运行 4 项检查并输出机器可读 artifact：

  1. checkpoint_cross_process  —— 实例 A 写 checkpoint，实例 B 读同一 thread
  2. same_thread_serialization —— 同 thread 跨 client 互斥
  3. different_thread_parallelism —— 不同 thread 可并发
  4. tool_idempotency          —— 重试不重复副作用

基础设施（未配置则对应检查 NOT_RUN）：

  TEST_DISTRIBUTED_DB_URL / DISTRIBUTED_DB_URL /                    (PostgreSQL)
  TEST_POSTGRES_CHECKPOINT_URL / DATABASE_URL
  TEST_REDIS_URL / REDIS_URL                                        (Redis)

``TEST_DISTRIBUTED_DB_URL`` / ``TEST_REDIS_URL`` 是与
``tests/integration/runtime/conftest.py``、``scripts/test_worker_crash_recovery.py``、
``make runtime-e2e|chaos|verify`` 和 CI 一致的**规范变量名**，优先级最高。

退出码（fail-closed）::

    0  全部检查 PASS
    1  有检查 FAIL（配置了基础设施但没通过）
    2  基础设施未配置（NOT_RUN）—— 默认**不算成功**

``NOT_RUN`` 默认返回 2 而不是 0：把"没跑"当成"通过"是证据污染。需要把它当成功
（例如只想生成占位 artifact）时显式传 ``--allow-not-run``。

输出：artifacts/distributed-runtime/<UTC 时间戳>/report.json
用法：
  python scripts/verify_distributed_runtime.py
  TEST_DISTRIBUTED_DB_URL=postgresql://... TEST_REDIS_URL=redis://... \
      python scripts/verify_distributed_runtime.py
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
SCHEMA_VERSION = "distributed-runtime-evidence/v2"


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

    # TEST_DISTRIBUTED_DB_URL 必须是第一候选：它就是 conftest / chaos 脚本 /
    # Makefile / CI 注入的规范变量名。之前这里漏了它，导致 make runtime-verify
    # 明明注入了 PG URL 却仍然报 NOT_RUN（而错误提示却让用户去设这个变量）。
    pg_url = _first_env(
        "TEST_DISTRIBUTED_DB_URL",
        "DISTRIBUTED_DB_URL",
        "TEST_POSTGRES_CHECKPOINT_URL",
        "DATABASE_URL",
    )
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
    values = set(statuses.values())
    if "FAIL" in values:
        overall = "FAIL"
    elif values == {"PASS"}:
        overall = "PASS"
    elif values == {"NOT_RUN"}:
        overall = "NOT_RUN"
    else:
        # 部分检查跑了、部分没跑：既不是全通过也不是全没跑，不能记成 PASS
        overall = "PARTIAL"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # 被测代码的 commit（生成 evidence 时的 HEAD，忽略未跟踪的 evidence 文件）。
        # artifact 自身随后被提交到另一个 commit（artifact_commit_sha），生成时无法预知。
        "tested_code_sha": _git_sha(),
        "artifact_commit_sha": None,
        "overall_status": overall,
        "checks": checks,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Distributed runtime 能力证据生成")
    parser.add_argument(
        "--allow-not-run",
        action="store_true",
        help="基础设施未配置（NOT_RUN）时也返回 0（默认返回 2，避免把没跑当成通过）",
    )
    args = parser.parse_args()

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
    status = report["overall_status"]
    if status == "PASS":
        return 0
    if status in ("NOT_RUN", "PARTIAL"):
        if args.allow_not_run:
            return 0
        print(
            json.dumps(
                {
                    "overall_status": "NOT_RUN",
                    "hint": "需要 TEST_DISTRIBUTED_DB_URL + TEST_REDIS_URL；"
                    "或用 make runtime-e2e / make runtime-chaos",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
