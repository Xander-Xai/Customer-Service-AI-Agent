"""Worker crash recovery —— **从 PostgreSQL checkpoint 续跑**（Gate 7 / P1）。

与 ``test_worker_crash_recovery.py`` 的区别（那一支证明的是 broker redelivery，
用的是 memory checkpointer，只能证明"重跑"）：本文件证明的是**断点续跑**。

真实组件：
  - PostgreSQL：AgentRun 真相源 + LangGraph AsyncPostgresSaver checkpoint；
  - Redis：Celery broker（acks_late + visibility_timeout）与记账；
  - 两个独立 Celery worker 进程（prefork，第二个在第一个被 SIGKILL 后启动）；
  - 真实 LangGraph StateGraph（two super-steps，第二个阻塞）。

流程::

    enqueue -> Worker A: first(完成并 checkpoint) -> second(阻塞)
    -> SIGKILL Worker A
    -> 未 ACK 消息经 visibility timeout 重新可见
    -> Worker B: 检测到未完成 checkpoint -> **跳过 first** -> 重做 second -> SUCCEEDED

验收（缺一即 FAIL）：
  1. 崩前 PostgreSQL 里确实存在 checkpoint（直接查 checkpoints 表）；
  2. ``ckpt:node_first == 1`` —— first **没有**被重新执行（不是从头重跑）；
  3. ``ckpt:node_second >= 2`` —— 未完成的那一步被重做；
  4. ``ckpt:recovered >= 1`` —— Worker B 自检到从断点恢复；
  5. 最终 status == SUCCEEDED，attempt >= 2，worker_id 已切换，单行无重复结果。

运行::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_worker_checkpoint_recovery.py -q
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

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (DB_URL and REDIS_URL),
        reason="TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis",
    ),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKER_CMD = [sys.executable, "-m", "tests.integration.celery_worker_runner"]
_PROVIDER = "tests.integration.checkpoint_resume_provider:provide"

_VISIBILITY_TIMEOUT = 5
_LEASE_SECONDS = 3
_BLOCK_SECONDS = 25

_CKPT_KEYS = (
    "ckpt:node_first",
    "ckpt:node_second",
    "ckpt:trace",
    "ckpt:recovered",
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
            # 每次测试用**独立队列**：否则上一次异常退出遗留的 worker（可能仍在
            # 消费 agent_runs）会抢走本次消息，让"崩溃恢复"变成"别人跑完了"。
            "AGENT_RUN_QUEUE": queue,
            "AGENT_RUN_VISIBILITY_TIMEOUT": str(_VISIBILITY_TIMEOUT),
            "AGENT_RUN_LEASE_SECONDS": str(_LEASE_SECONDS),
            "AGENT_RUN_HEARTBEAT_SECONDS": "1",
            "AGENT_RUN_THREAD_LOCK_TTL_SECONDS": str(_LEASE_SECONDS),
            "AGENT_RUN_THREAD_LOCK_BACKEND": "redis",
            "AGENT_RUN_RUNTIME_PROVIDER": _PROVIDER,
            "CKPT_BLOCK_SECONDS": str(_BLOCK_SECONDS),
            # 关键：必须是 postgres。memory 只能证明重跑，证明不了续跑。
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
        # 独立会话：必须能对**整个进程组**发信号。Celery 是 prefork，只 kill 父进程
        # 会留下正在执行任务的孤儿子进程继续跑完 —— 那样测到的就不是崩溃恢复，
        # 而是「父进程没了但任务照常完成」。
        start_new_session=True,
    )
    proc._csai_log = log  # type: ignore[attr-defined]
    return proc


def _kill_worker_group(proc: subprocess.Popen) -> None:
    """SIGKILL 整个进程组（父 + prefork 子），模拟 worker 进程被强杀。"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=15)


def _stop_worker(proc: subprocess.Popen) -> None:
    try:
        _kill_worker_group(proc)
    finally:
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
def test_worker_crash_resumes_from_postgres_checkpoint():
    import redis as redis_lib
    from sqlalchemy import create_engine, text

    from db.models import AgentDeadLetter, AgentRun, ToolSideEffect

    engine = create_engine(DB_URL)
    for table in (AgentRun, AgentDeadLetter, ToolSideEffect):
        table.__table__.create(engine, checkfirst=True)

    tag = uuid.uuid4().hex[:8]
    run_id = f"ckpt-crash-{tag}"
    thread_id = f"T-ckpt-crash-{tag}"

    # 独立队列：隔离本次测试与任何遗留 worker
    queue = f"agent_runs_test_{tag}"

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.delete("unacked", "unacked_index", queue, *_CKPT_KEYS)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runs "
                "(id, thread_id, session_id, status, query, attempt, max_attempts, "
                " created_at, queued_at, updated_at) "
                "VALUES (:id, :tid, :tid, 'QUEUED', 'ckpt-crash', 0, 3, now(), now(), now())"
            ),
            {"id": run_id, "tid": thread_id},
        )

    from celery import Celery

    Celery(broker=REDIS_URL).send_task(
        "runtime.execute_agent_run", args=[run_id], queue=queue
    )

    def row():
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT status, attempt, worker_id, result FROM agent_runs WHERE id=:id"
                ),
                {"id": run_id},
            ).fetchone()

    def checkpoint_rows() -> int:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
                    {"t": thread_id},
                ).scalar()
                or 0
            )

    env = _worker_env(queue)
    worker_a = _start_worker(env, "/tmp/csai-ckpt-worker-a.log")
    worker_b = None
    try:
        # 1) 等 Worker A 跑完 first（已 checkpoint）并阻塞在 second
        entered_second = _wait_for(
            lambda: int(client.get("ckpt:node_second") or 0) >= 1, 60
        )
        assert entered_second, "Worker A 未进入 second 节点"

        # 2) 崩前必须已经存在 PostgreSQL checkpoint（硬证据）
        ckpts_before_crash = _wait_for(lambda: checkpoint_rows() >= 1, 20)
        assert ckpts_before_crash, (
            "崩溃前 PostgreSQL checkpoints 表为空，无法证明断点续跑"
        )
        first_before = int(client.get("ckpt:node_first") or 0)
        assert first_before == 1, f"first 应恰好执行一次，实际 {first_before}"

        running = _wait_for(
            lambda: (r := row()) is not None and r[0] == "RUNNING" and r, 20
        )
        assert running, f"未进入 RUNNING: {row()}"
        worker_a_id = running[2]
        assert worker_a_id

        # 3) SIGKILL 执行中的 worker（整个进程组，含 prefork 子进程）
        _stop_worker(worker_a)

        # 4) 等未 ACK 消息超过 visibility_timeout，启动 Worker B 触发 redelivery
        time.sleep(_VISIBILITY_TIMEOUT + 3)
        worker_b = _start_worker(env, "/tmp/csai-ckpt-worker-b.log")

        final = _wait_for(
            lambda: (r := row()) is not None
            and r[0] in ("SUCCEEDED", "FAILED", "DEAD_LETTER")
            and r,
            150,
        )
        assert final is not None, f"run 未进入终态: {row()}"
        assert final[0] == "SUCCEEDED", f"未恢复成功: {final}"

        # 5) 断点续跑证据：first 未被重跑，second 被重做
        first_after = int(client.get("ckpt:node_first") or 0)
        second_after = int(client.get("ckpt:node_second") or 0)
        recovered = int(client.get("ckpt:recovered") or 0)
        trace = client.lrange("ckpt:trace", 0, -1) or []

        assert first_after == 1, (
            f"first 被执行 {first_after} 次 —— 说明是从头重跑而非从 checkpoint 续跑；trace={trace}"
        )
        assert second_after >= 2, f"second 应至少执行 2 次（崩溃后重做）：{second_after}"
        assert recovered >= 1, f"Worker B 未检测到未完成 checkpoint：trace={trace}"

        # 6) 执行归属切换 + 无重复最终结果
        assert final[1] >= 2, f"应发生重新执行 (attempt>=2): {final}"
        assert final[2] != worker_a_id, "接管 worker 应与被杀 worker 不同"
        assert final[3] and final[3].get("stage") == "second"
        assert final[3].get("resumed") is True, "最终执行未标记为断点续跑"

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
            conn.execute(
                text("DELETE FROM agent_dead_letters WHERE run_id=:id"), {"id": run_id}
            )
        client.delete(*_CKPT_KEYS, queue)
        client.close()
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM checkpoints WHERE thread_id = :t"), {"t": thread_id}
            )
