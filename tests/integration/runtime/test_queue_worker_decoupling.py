"""Gate 6：API / Worker 解耦（真实 Celery + Redis broker + PostgreSQL）。

验收要求::

    FastAPI 进程 -> persist run -> enqueue run_id -> 立即返回（202）
    无 worker 时：run 必须保持 QUEUED（attempt=0, started_at 为空）
                 —— 证明 API 进程**没有**偷偷执行 graph
    启动 worker  ：任务被消费并执行完成

``dispatch_run`` 与 ``POST /api/runs`` 走的是同一条入队路径，这里直接复用
``runtime.dispatch``，因此覆盖的是生产代码而非替身。

运行::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_queue_worker_decoupling.py -q
"""

from __future__ import annotations

import contextlib
import json
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
_PROVIDER = "tests.integration.fake_runtime_provider:provide"


def _worker_env(queue: str, sleep: float) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": DB_URL,
            "REDIS_URL": REDIS_URL,
            "DEV_MODE": "true",
            "AGENT_RUN_DISPATCH": "celery",
            "AGENT_RUN_QUEUE": queue,
            "AGENT_RUN_VISIBILITY_TIMEOUT": "60",
            "AGENT_RUN_LEASE_SECONDS": "60",
            "AGENT_RUN_HEARTBEAT_SECONDS": "5",
            "AGENT_RUN_THREAD_LOCK_TTL_SECONDS": "120",
            "AGENT_RUN_THREAD_LOCK_BACKEND": "redis",
            "AGENT_RUN_RUNTIME_PROVIDER": _PROVIDER,
            "FAKE_RUNTIME_SLEEP_SECONDS": str(sleep),
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


@pytest.mark.timeout(300)
def test_gate6_api_enqueues_and_worker_executes():
    """无 worker -> 保持 QUEUED；启动 worker -> 被消费执行。"""
    import redis as redis_lib
    from sqlalchemy import create_engine, text

    from db.models import AgentDeadLetter, AgentRun, ToolSideEffect

    engine = create_engine(DB_URL)
    for table in (AgentRun, AgentDeadLetter, ToolSideEffect):
        table.__table__.create(engine, checkfirst=True)

    tag = uuid.uuid4().hex[:8]
    run_id = f"decouple-{tag}"
    thread_id = f"T-decouple-{tag}"
    queue = f"agent_runs_test_{tag}"

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.delete("unacked", "unacked_index", queue, "crash-test:executions")

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runs "
                "(id, thread_id, session_id, status, query, attempt, max_attempts, "
                " created_at, queued_at, updated_at) "
                "VALUES (:id, :tid, :tid, 'QUEUED', 'decouple', 0, 3, now(), now(), now())"
            ),
            {"id": run_id, "tid": thread_id},
        )

    worker = None
    try:
        # --- API 侧只做「落库 + 入队」（独立进程，celery 模式）--------
        proc = subprocess.run(
            [
                sys.executable,
                str(_REPO_ROOT / "tests" / "integration" / "runtime" / "enqueue_subprocess.py"),
                run_id,
                queue,
                DB_URL,
                REDIS_URL,
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert proc.returncode == 0, f"入队子进程失败: {proc.stderr[-2000:]}"
        enqueued = json.loads(
            [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")][-1]
        )
        assert (
            enqueued["mode"] == "celery"
        ), f"API 进程必须处于 celery 模式，实际 {enqueued['mode']}"
        assert enqueued["task_id"], "未拿到 Celery task_id"

        # 消息确实在 broker 队列里
        qlen = client.llen(queue)
        assert qlen >= 1, f"消息未进入 broker 队列 {queue}（llen={qlen}）"

        # --- 没有任何 worker：必须保持 QUEUED，绝不被 API 进程执行 ----
        time.sleep(6)
        row = _row(engine, run_id)
        assert row[0] == "QUEUED", f"无 worker 时 run 不应被执行，实际 {row}"
        assert row[1] == 0, f"attempt 不应增长，实际 {row}"
        assert row[2] is None, f"started_at 不应被写入（说明确实没执行），实际 {row}"
        assert int(client.get("crash-test:executions") or 0) == 0, "无 worker 时不应发生任何图执行"

        # --- 启动 worker：任务应被消费并完成 --------------------------
        worker = _start_worker(_worker_env(queue, 1.0), "/tmp/csai-decouple-worker.log")
        final = _wait_terminal(engine, run_id, timeout=90)
        assert final[0] == "SUCCEEDED", f"worker 未消费成功: {final}"
        assert final[1] >= 1
        assert final[2] is not None, "worker_id 应被写入（证明是 worker 执行而非 API）"
        assert final[3] is not None, "finished_at 应被写入"
        assert int(client.get("crash-test:executions") or 0) >= 1
    finally:
        if worker is not None:
            _stop_worker(worker)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_runs WHERE id=:id"), {"id": run_id})
            conn.execute(text("DELETE FROM agent_dead_letters WHERE run_id=:id"), {"id": run_id})
        client.delete("crash-test:executions", queue, "unacked", "unacked_index")
        client.close()


def _row(engine, run_id: str):
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, attempt, started_at, finished_at, worker_id "
                "FROM agent_runs WHERE id=:id"
            ),
            {"id": run_id},
        ).fetchone()


def _wait_terminal(engine, run_id: str, timeout: float):
    terminal = ("SUCCEEDED", "FAILED", "DEAD_LETTER", "CANCELLED")
    deadline = time.time() + timeout
    row = _row(engine, run_id)
    while time.time() < deadline and row[0] not in terminal:
        time.sleep(0.3)
        row = _row(engine, run_id)
    return row
