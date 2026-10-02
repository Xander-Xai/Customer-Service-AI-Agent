"""Gate 11：副作用工具的幂等保护（**关键验收**，失败即整体 FAIL）。

场景（at-least-once delivery 无法避免重复投递，所以副作用必须自身幂等）::

    Worker A: 执行副作用工具 -> 外部系统已写入（counter=1）
              -> 工具成功、但消息还没 ACK 时被 SIGKILL
    Broker:   未 ACK 消息重新投递
    Worker B: 从 checkpoint 续跑，重做该节点
              -> ledger 命中 SUCCEEDED -> 返回历史结果
              -> 外部系统**不得**被再次写入

验收：
  1. ``idem:counter == 1``（绝不能是 2）；
  2. ``tool_side_effects`` 里存在该 ``(tool_name, operation_key)`` 且 status=SUCCEEDED；
  3. 重投确实发生过（``idem:tool_invocations >= 2``、``idem:ledger_hits >= 1``）——
     否则 counter==1 只是因为压根没重跑；
  4. 最终 run SUCCEEDED。

``agent_tool_idempotency_hit_total`` 的递增由同目录
``test_tool_idempotency_metric.py`` 在进程内断言（worker 进程的 Prometheus
registry 外部无法读取）。

运行::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_tool_idempotency.py -q
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (DB_URL and REDIS_URL), reason=INFRA_REASON),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKER_CMD = [sys.executable, "-m", "tests.integration.celery_worker_runner"]
_PROVIDER = "tests.integration.side_effect_provider:provide"

_VISIBILITY_TIMEOUT = 5
_BLOCK_SECONDS = 25

_REDIS_KEYS = (
    "idem:counter",
    "idem:ledger_hits",
    "idem:phase",
    "idem:tool_invocations",
)


def _worker_env(queue: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": DB_URL,
            "LANGGRAPH_CHECKPOINT_DATABASE_URL": DB_URL,
            "REDIS_URL": REDIS_URL,
            "DEV_MODE": "true",
            "AGENT_RUN_DISPATCH": "celery",
            "AGENT_RUN_QUEUE": queue,
            "AGENT_RUN_VISIBILITY_TIMEOUT": str(_VISIBILITY_TIMEOUT),
            "AGENT_RUN_LEASE_SECONDS": "3",
            "AGENT_RUN_HEARTBEAT_SECONDS": "1",
            "AGENT_RUN_THREAD_LOCK_TTL_SECONDS": "3",
            "AGENT_RUN_THREAD_LOCK_BACKEND": "redis",
            "AGENT_RUN_RUNTIME_PROVIDER": _PROVIDER,
            "SIDE_EFFECT_BLOCK_SECONDS": str(_BLOCK_SECONDS),
            "LANGGRAPH_CHECKPOINT_BACKEND": "postgres",
        }
    )
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def _start_worker(env: dict[str, str], log_path: str) -> subprocess.Popen:
    log = open(log_path, "w")  # noqa: SIM115 - closed in _stop_worker
    proc = subprocess.Popen(
        _WORKER_CMD,
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._csai_log = log  # type: ignore[attr-defined]
    return proc


def _stop_worker(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=15)
    log = getattr(proc, "_csai_log", None)
    if log is not None:
        log.close()


def _wait_for(predicate, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.3)
    return predicate()


@pytest.mark.timeout(300)
def test_gate11_side_effect_tool_executes_exactly_once_across_worker_kill():
    import redis as redis_lib
    from sqlalchemy import create_engine, text

    from db.models import AgentDeadLetter, AgentRun, ToolSideEffect

    engine = create_engine(DB_URL)
    for table in (AgentRun, AgentDeadLetter, ToolSideEffect):
        table.__table__.create(engine, checkfirst=True)

    tag = uuid.uuid4().hex[:8]
    run_id = f"idem-{tag}"
    thread_id = f"T-idem-{tag}"
    queue = f"agent_runs_test_{tag}"

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.delete("unacked", "unacked_index", queue, *_REDIS_KEYS)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runs "
                "(id, thread_id, session_id, status, query, attempt, max_attempts, "
                " created_at, queued_at, updated_at) "
                "VALUES (:id, :tid, :tid, 'QUEUED', 'refund-me', 0, 3, now(), now(), now())"
            ),
            {"id": run_id, "tid": thread_id},
        )

    from celery import Celery

    Celery(broker=REDIS_URL).send_task(
        "runtime.execute_agent_run", args=[run_id], queue=queue
    )

    def counter():
        return int(client.get("idem:counter") or 0)

    def status():
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT status, attempt, worker_id FROM agent_runs WHERE id=:id"),
                {"id": run_id},
            ).fetchone()

    env = _worker_env(queue)
    worker_a = _start_worker(env, "/tmp/csai-idem-worker-a.log")
    worker_b = None
    try:
        # 1) 等副作用真实落地（counter == 1），此时工具已成功但图还在阻塞
        landed = _wait_for(lambda: counter() >= 1, 90)
        assert landed, "副作用工具从未执行"
        assert counter() == 1, f"崩前 counter 应为 1，实际 {counter()}"

        # ledger 已记录 SUCCEEDED
        ledger = _wait_for(
            lambda: _ledger_row(engine, thread_id) is not None
            and _ledger_row(engine, thread_id)[2] == "SUCCEEDED",
            20,
        )
        assert ledger, "ledger 未记录 SUCCEEDED"

        # 2) 工具成功、消息未 ACK 时 SIGKILL
        _stop_worker(worker_a)

        # 3) 等未 ACK 消息重新可见，启动 Worker B
        time.sleep(_VISIBILITY_TIMEOUT + 3)
        worker_b = _start_worker(env, "/tmp/csai-idem-worker-b.log")

        final = _wait_for(
            lambda: (r := status()) is not None
            and r[0] in ("SUCCEEDED", "FAILED", "DEAD_LETTER")
            and r,
            150,
        )
        assert final is not None, f"run 未进入终态: {status()}"
        assert final[0] == "SUCCEEDED", f"未恢复成功: {final}"
        assert final[1] >= 2, f"应发生重投重做，实际 attempt={final[1]}"

        # 4) 关键断言：副作用只发生了一次
        final_counter = counter()
        assert final_counter == 1, (
            f"副作用被重复执行！counter={final_counter}（必须 == 1）"
        )

        # 5) 并且确实重投过（否则 counter==1 只是因为没重跑）
        invocations = int(client.get("idem:tool_invocations") or 0)
        hits = int(client.get("idem:ledger_hits") or 0)
        assert invocations >= 2, f"工具应被再次调用，实际 {invocations} 次"
        assert hits >= 1, f"第二次调用应命中 ledger，实际命中 {hits} 次"

        # 6) ledger 只有一条 SUCCEEDED 记录（operation_key = run_id:tool_call_id）
        row = _ledger_row(engine, thread_id)
        assert row is not None
        assert row[0] == "increment_counter"
        assert row[1] == f"{run_id}:{thread_id}:refund"
        assert row[2] == "SUCCEEDED"
        with engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM tool_side_effects "
                    "WHERE run_id=:rid AND tool_name='increment_counter'"
                ),
                {"rid": run_id},
            ).scalar()
        assert count == 1, f"ledger 不应出现重复行，实际 {count}"
    finally:
        if worker_b is not None:
            _stop_worker(worker_b)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_runs WHERE id=:id"), {"id": run_id})
            conn.execute(
                text("DELETE FROM agent_dead_letters WHERE run_id=:id"), {"id": run_id}
            )
            conn.execute(
                text("DELETE FROM tool_side_effects WHERE run_id=:id"), {"id": run_id}
            )
            conn.execute(
                text("DELETE FROM checkpoints WHERE thread_id = :t"), {"t": thread_id}
            )
        client.delete(*_REDIS_KEYS, queue, "unacked", "unacked_index")
        client.close()


def _ledger_row(engine, thread_id: str):
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT tool_name, operation_key, status FROM tool_side_effects "
                "WHERE thread_id=:t LIMIT 1"
            ),
            {"t": thread_id},
        ).fetchone()
