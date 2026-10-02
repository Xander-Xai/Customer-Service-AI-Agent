"""Worker crash / broker redelivery 恢复集成测试（Gate 7）。

真实组件：
  - Redis broker（Celery, acks_late + visibility_timeout）；
  - PostgreSQL AgentRun 真相源；
  - 两个独立 Celery worker 进程（prefork）；
  - deterministic fake runtime（不调用真实 Provider）。

流程：
  enqueue -> Worker A 领取并进入 RUNNING -> SIGKILL Worker A
  -> 未 ACK 任务经 broker visibility timeout 重新可见
  -> Worker B 领取（接管过期 lease）-> 最终 SUCCEEDED

验收：任务最终 SUCCEEDED；不产生两个最终结果；能证明消息 redelivery；
fake graph 被重新执行（记录实际恢复边界）。

默认跳过。运行：

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/cosmetics_ai \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/test_worker_crash_recovery.py -q

等价脚本：``scripts/repro_worker_crash_recovery.sh``。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (DB_URL and REDIS_URL),
        reason="TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis",
    ),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKER_CMD = [sys.executable, "-m", "tests.integration.celery_worker_runner"]
_VISIBILITY_TIMEOUT = 5
_LEASE_SECONDS = 3
_FAKE_SLEEP = 6


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": DB_URL,
            "REDIS_URL": REDIS_URL,
            "DEV_MODE": "true",
            "AGENT_RUN_DISPATCH": "celery",
            "AGENT_RUN_VISIBILITY_TIMEOUT": str(_VISIBILITY_TIMEOUT),
            "AGENT_RUN_LEASE_SECONDS": str(_LEASE_SECONDS),
            "AGENT_RUN_HEARTBEAT_SECONDS": "1",
            "AGENT_RUN_THREAD_LOCK_TTL_SECONDS": str(_LEASE_SECONDS),
            "AGENT_RUN_THREAD_LOCK_BACKEND": "redis",
            "AGENT_RUN_RUNTIME_PROVIDER": ("tests.integration.fake_runtime_provider:provide"),
            "FAKE_RUNTIME_SLEEP_SECONDS": str(_FAKE_SLEEP),
            "LANGGRAPH_CHECKPOINT_BACKEND": "memory",
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
    )
    proc._csai_log = log  # type: ignore[attr-defined]
    return proc


def _stop_worker(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
        proc.wait(timeout=10)
    finally:
        log = getattr(proc, "_csai_log", None)
        if log is not None:
            log.close()


@pytest.mark.timeout(180)
def test_worker_killed_mid_run_is_redelivered_and_succeeds():
    import redis as redis_lib
    from celery import Celery
    from sqlalchemy import create_engine, text

    from db.models import AgentDeadLetter, AgentRun, ToolSideEffect

    engine = create_engine(DB_URL)
    for table in (AgentRun, AgentDeadLetter, ToolSideEffect):
        table.__table__.create(engine, checkfirst=True)

    tag = uuid.uuid4().hex[:8]
    run_id = f"crash-{tag}"
    thread_id = f"T-crash-{tag}"

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    # 隔离：清掉可能残留的队列/未确认消息
    client.delete("unacked", "unacked_index", "agent_runs", "crash-test:executions")

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runs "
                "(id, thread_id, session_id, status, query, attempt, max_attempts, "
                " created_at, queued_at, updated_at) "
                "VALUES (:id, :tid, :tid, 'QUEUED', 'crash-test', 0, 3, now(), now(), now())"
            ),
            {"id": run_id, "tid": thread_id},
        )

    Celery(broker=REDIS_URL).send_task(
        "runtime.execute_agent_run", args=[run_id], queue="agent_runs"
    )

    def status():
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT status, attempt, worker_id, result FROM agent_runs WHERE id=:id"),
                {"id": run_id},
            ).fetchone()

    def wait_for(predicate, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = status()
            if predicate(row):
                return row
            time.sleep(0.3)
        return status()

    env = _worker_env()
    worker_a = _start_worker(env, "/tmp/csai-worker-a.log")
    worker_b = None
    try:
        running = wait_for(lambda r: r and r[0] == "RUNNING", 40)
        assert running is not None and running[0] == "RUNNING", f"A 未进入 RUNNING: {running}"
        worker_a_id = running[2]
        assert worker_a_id

        # 强制杀掉正在执行的 worker A（SIGKILL）
        _stop_worker(worker_a)

        # 等未 ACK 消息超过 visibility_timeout，再启动 worker B 触发 redelivery
        time.sleep(_VISIBILITY_TIMEOUT + 3)
        worker_b = _start_worker(env, "/tmp/csai-worker-b.log")

        final = wait_for(lambda r: r and r[0] in ("SUCCEEDED", "FAILED", "DEAD_LETTER"), 90)
        assert final is not None
        assert final[0] == "SUCCEEDED", f"未恢复成功: {final}"

        # 不产生两个最终结果：单行、result 唯一、worker 已切换
        assert final[1] >= 2, f"应发生重新执行 (attempt>=2): {final}"
        assert final[2] != worker_a_id, "接管 worker 应与被杀 worker 不同"
        assert final[3] and final[3].get("response", "").startswith("fake-ok")

        # 证明 fake graph 被重新执行（恢复边界）
        executions = int(client.get("crash-test:executions") or 0)
        assert executions >= 2, f"fake runtime 执行次数应 >=2: {executions}"

        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM agent_runs WHERE id=:id"), {"id": run_id}
            ).scalar()
        assert count == 1
    finally:
        if worker_b is not None:
            _stop_worker(worker_b)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_runs WHERE id=:id"), {"id": run_id})
            conn.execute(text("DELETE FROM agent_dead_letters WHERE run_id=:id"), {"id": run_id})
        client.delete("crash-test:executions")
        client.close()
