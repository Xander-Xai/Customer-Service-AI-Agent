#!/usr/bin/env python3
"""Worker 崩溃恢复混沌验收脚本（Gate 16 / ``make runtime-chaos``）。

流程（真实 PostgreSQL + 真实 Redis + 真实 Celery + 两个真实 worker 进程）::

    1. 创建任务并入队
    2. Worker A 领取，运行到副作用工具之后（图尚未完成）
    3. SIGKILL **整个进程组**（父 + prefork 子）
    4. 等 lease/visibility 超时
    5. 启动 Worker B
    6. 任务从 PostgreSQL checkpoint 恢复并完成
    7. 副作用工具**只调用一次**

输出结构化证据 JSON 到 stdout，并以退出码表达结论：
  0 = PASS   2 = FAIL（附失败原因）

用法::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    python scripts/test_worker_crash_recovery.py
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

WORKER_CMD = [sys.executable, "-m", "tests.integration.celery_worker_runner"]
PROVIDER = "tests.integration.side_effect_provider:provide"

VISIBILITY_TIMEOUT = 5
LEASE_SECONDS = 3
BLOCK_SECONDS = 25


def redis_keys(prefix: str) -> tuple[str, ...]:
    """本次 run 专用的 provider Redis key。

    provider 通过 ``SIDE_EFFECT_KEY_PREFIX`` 决定前缀。固定用全局 ``idem:*``
    会让本脚本与并发/残留的 run 共享同一组 key，``ledger_hits`` 断言因此依赖
    「机器上没有别人碰过这些 key」这一无法保证的前提。
    """
    return (
        f"{prefix}:counter",
        f"{prefix}:ledger_hits",
        f"{prefix}:phase",
        f"{prefix}:tool_invocations",
    )


class ChaosFailure(RuntimeError):
    pass


def worker_env(queue: str, key_prefix: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": DB_URL,
            "LANGGRAPH_CHECKPOINT_DATABASE_URL": DB_URL,
            "REDIS_URL": REDIS_URL,
            "DEV_MODE": "true",
            "AGENT_RUN_DISPATCH": "celery",
            "AGENT_RUN_QUEUE": queue,
            "AGENT_RUN_VISIBILITY_TIMEOUT": str(VISIBILITY_TIMEOUT),
            "AGENT_RUN_LEASE_SECONDS": str(LEASE_SECONDS),
            "AGENT_RUN_HEARTBEAT_SECONDS": "1",
            "AGENT_RUN_THREAD_LOCK_TTL_SECONDS": str(LEASE_SECONDS),
            "AGENT_RUN_THREAD_LOCK_BACKEND": "redis",
            "AGENT_RUN_RUNTIME_PROVIDER": PROVIDER,
            "SIDE_EFFECT_BLOCK_SECONDS": str(BLOCK_SECONDS),
            "SIDE_EFFECT_KEY_PREFIX": key_prefix,
            "LANGGRAPH_CHECKPOINT_BACKEND": "postgres",
        }
    )
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def start_worker(env: dict[str, str], log_path: Path) -> subprocess.Popen:
    log = log_path.open("w")
    proc = subprocess.Popen(
        WORKER_CMD,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._log = log  # type: ignore[attr-defined]
    return proc


def kill_worker_group(proc: subprocess.Popen) -> None:
    """SIGKILL 整个进程组。仅 kill 父进程会留下 prefork 子进程继续跑完任务，
    那样测到的就不是崩溃恢复。"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=15)
    log = getattr(proc, "_log", None)
    if log is not None:
        log.close()


def wait_for(predicate, timeout: float, label: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.3)
    raise ChaosFailure(f"等待超时（{timeout}s）：{label}")


def run() -> dict:
    if not DB_URL or not REDIS_URL:
        raise ChaosFailure(
            "缺少 TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL；chaos 需要真实 PG + Redis"
        )

    import redis as redis_lib
    from sqlalchemy import create_engine, text

    from db.models import AgentDeadLetter, AgentRun, ToolSideEffect

    engine = create_engine(DB_URL)
    for table in (AgentRun, AgentDeadLetter, ToolSideEffect):
        table.__table__.create(engine, checkfirst=True)

    tag = uuid.uuid4().hex[:8]
    run_id = f"chaos-{tag}"
    thread_id = f"T-chaos-{tag}"
    queue = f"agent_runs_test_{tag}"
    key_prefix = f"idem:{tag}"

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    keys = redis_keys(key_prefix)
    client.delete("unacked", "unacked_index", queue, *keys)

    steps: list[dict] = []
    worker_a = None
    worker_b = None
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_runs (id, thread_id, session_id, status, query, "
                    "attempt, max_attempts, created_at, queued_at, updated_at) "
                    "VALUES (:id, :tid, :tid, 'QUEUED', 'chaos', 0, 3, now(), now(), now())"
                ),
                {"id": run_id, "tid": thread_id},
            )
        steps.append({"step": 1, "action": "create_run", "run_id": run_id, "ok": True})

        from celery import Celery

        Celery(broker=REDIS_URL).send_task("runtime.execute_agent_run", args=[run_id], queue=queue)
        assert client.llen(queue) >= 1, "消息未进入 broker 队列"
        steps.append({"step": 2, "action": "enqueue", "queue": queue, "ok": True})

        env = worker_env(queue, key_prefix)
        worker_a = start_worker(env, Path("/tmp/csai-chaos-worker-a.log"))

        counter = lambda: int(client.get(f"{key_prefix}:counter") or 0)  # noqa: E731
        wait_for(lambda: counter() >= 1, 90, "副作用工具执行")
        if counter() != 1:
            raise ChaosFailure(f"崩前副作用应恰好 1 次，实际 {counter()}")
        steps.append(
            {
                "step": 3,
                "action": "side_effect_applied",
                "idem:counter": counter(),
                "ok": True,
            }
        )

        row = wait_for(
            lambda: (r := _row(engine, run_id)) is not None and r[0] == "RUNNING" and r,
            30,
            "Worker A 进入 RUNNING",
        )
        worker_a_id = row[2]  # (status, attempt, worker_id)
        if not worker_a_id:
            raise ChaosFailure("Worker A 未写入 worker_id")
        ckpts = _checkpoint_count(engine, thread_id)
        if ckpts < 1:
            raise ChaosFailure("崩溃前 PostgreSQL 不存在 checkpoint，无法验证续跑")
        steps.append(
            {
                "step": 4,
                "action": "checkpoint_persisted",
                "checkpoints": ckpts,
                "worker_id": worker_a_id,
                "ok": True,
            }
        )

        kill_worker_group(worker_a)
        worker_a = None
        steps.append({"step": 5, "action": "sigkill_worker_group", "ok": True})

        time.sleep(VISIBILITY_TIMEOUT + 3)
        worker_b = start_worker(env, Path("/tmp/csai-chaos-worker-b.log"))
        steps.append({"step": 6, "action": "restart_worker", "ok": True})

        final = wait_for(
            lambda: (r := _row(engine, run_id))
            and r[0] in ("SUCCEEDED", "FAILED", "DEAD_LETTER")
            and r,
            150,
            "run 进入终态",
        )
        if final[0] != "SUCCEEDED":
            raise ChaosFailure(f"任务未恢复成功：{final}")
        steps.append(
            {
                "step": 7,
                "action": "recovered",
                "status": final[0],
                "attempt": final[1],
                "worker_id": final[2],
                "worker_switched": final[2] != worker_a_id,
                "ok": True,
            }
        )

        final_counter = counter()
        invocations = int(client.get(f"{key_prefix}:tool_invocations") or 0)
        hits = int(client.get(f"{key_prefix}:ledger_hits") or 0)
        ledger = _ledger(engine, thread_id)

        if final_counter != 1:
            raise ChaosFailure(f"副作用被重复执行：idem:counter={final_counter}（必须 == 1）")
        if invocations < 2:
            raise ChaosFailure(f"工具未被重新调用（{invocations} 次），未验证幂等去重")
        if hits < 1:
            raise ChaosFailure("第二次调用未命中 ledger 幂等路径")
        if not ledger or ledger[2] != "SUCCEEDED":
            raise ChaosFailure(f"ledger 记录异常: {ledger}")
        steps.append(
            {
                "step": 8,
                "action": "side_effect_deduplicated",
                "idem:counter": final_counter,
                "tool_invocations": invocations,
                "ledger_hits": hits,
                "ledger": {
                    "tool_name": ledger[0],
                    "operation_key": ledger[1],
                    "status": ledger[2],
                },
                "ok": True,
            }
        )

        return {
            "result": "PASS",
            "run_id": run_id,
            "thread_id": thread_id,
            "queue": queue,
            "steps": steps,
        }
    except Exception as e:
        return {
            "result": "FAIL",
            "run_id": run_id,
            "thread_id": thread_id,
            "error": f"{type(e).__name__}: {e}",
            "steps": steps,
        }
    finally:
        if worker_a is not None:
            kill_worker_group(worker_a)
        if worker_b is not None:
            kill_worker_group(worker_b)
        with contextlib.suppress(Exception), engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_runs WHERE id=:id"), {"id": run_id})
            conn.execute(text("DELETE FROM agent_dead_letters WHERE run_id=:id"), {"id": run_id})
            conn.execute(text("DELETE FROM tool_side_effects WHERE run_id=:id"), {"id": run_id})
            conn.execute(text("DELETE FROM checkpoints WHERE thread_id = :t"), {"t": thread_id})
        with contextlib.suppress(Exception):
            client.delete(*keys, queue, "unacked", "unacked_index")
            client.close()


def _row(engine, run_id: str):
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(
            text("SELECT status, attempt, worker_id FROM agent_runs WHERE id=:id"),
            {"id": run_id},
        ).fetchone()


def _checkpoint_count(engine, thread_id: str) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
                {"t": thread_id},
            ).scalar()
            or 0
        )


def _ledger(engine, thread_id: str):
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT tool_name, operation_key, status FROM tool_side_effects "
                "WHERE thread_id=:t LIMIT 1"
            ),
            {"t": thread_id},
        ).fetchone()


def main() -> int:
    parser = argparse.ArgumentParser(description="Worker 崩溃恢复混沌验收")
    parser.add_argument("--output", default="", help="把证据 JSON 写入该文件")
    args = parser.parse_args()

    report = run()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(f"\n[{'PASS' if report['result'] == 'PASS' else 'FAIL'}] runtime-chaos")
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
