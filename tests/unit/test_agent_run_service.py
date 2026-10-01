"""AgentRun 服务层 + 执行器（worker）单元测试。

覆盖任务要求的：create_run / run_state_transition / worker_success /
worker_failure / worker_retry / cancel_before_start / invalid_transition。

这些是本地单元测试（独立 SQLite + fake runtime），不依赖 Redis/Celery broker。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base
from runtime.executor import execute_run
from runtime.repository import AgentRunRepository
from runtime.run_service import RunNotFound, RunService
from runtime.statuses import InvalidRunTransition, RunStatus
from runtime.thread_lock import NullThreadLock


@pytest.fixture()
def service():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return RunService(AgentRunRepository(session_factory))


class _FakeRuntime:
    def __init__(self, *, result=None, error=None):
        self._result = result or {"response": "ok", "current_agent": "general_agent"}
        self._error = error
        self.calls = []

    async def run(self, *, thread_id, query, user_id=None):
        self.calls.append({"thread_id": thread_id, "query": query, "user_id": user_id})
        if self._error is not None:
            raise self._error
        return dict(self._result)


def _provider(runtime):
    async def _p():
        return runtime

    return _p


# ---------------------------------------------------------------------------
# 1. create_run / 状态迁移
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_run(service):
    run = service.create_run(query="你好", session_id="s1", user_id="u1")
    assert run["status"] == RunStatus.PENDING.value
    assert run["thread_id"] == "s1"  # thread_id == session_id
    assert run["session_id"] == "s1"
    assert run["attempt"] == 0
    assert run["max_attempts"] >= 1
    assert service.get_run(run["id"])["id"] == run["id"]


@pytest.mark.unit
def test_run_state_transition(service):
    run = service.create_run(query="q", session_id="s1")
    rid = run["id"]

    queued = service.mark_queued(rid)
    assert queued["status"] == RunStatus.QUEUED.value
    assert queued["queued_at"] is not None

    running = service.mark_running(rid)
    assert running["status"] == RunStatus.RUNNING.value
    assert running["started_at"] is not None

    succeeded = service.mark_succeeded(rid, {"response": "done"})
    assert succeeded["status"] == RunStatus.SUCCEEDED.value
    assert succeeded["result"] == {"response": "done"}
    assert succeeded["finished_at"] is not None


@pytest.mark.unit
def test_invalid_transition(service):
    run = service.create_run(query="q", session_id="s1")
    rid = run["id"]
    service.mark_queued(rid)
    service.mark_running(rid)
    service.mark_failed(rid, error_code="Boom", error_message="boom")

    # 不允许 FAILED -> RUNNING
    with pytest.raises(InvalidRunTransition):
        service.mark_running(rid)

    # 不允许 SUCCEEDED -> RUNNING
    run2 = service.create_run(query="q2", session_id="s2")
    service.mark_queued(run2["id"])
    service.mark_running(run2["id"])
    service.mark_succeeded(run2["id"], {})
    with pytest.raises(InvalidRunTransition):
        service.mark_running(run2["id"])


@pytest.mark.unit
def test_retry_run_and_dead(service):
    run = service.create_run(query="q", session_id="s1", max_attempts=3)
    rid = run["id"]
    service.mark_queued(rid)
    service.mark_running(rid)
    service.mark_failed(rid, error_code="E", error_message="e")

    retried = service.retry_run(rid)
    assert retried["status"] == RunStatus.QUEUED.value
    assert retried["attempt"] == 1

    # 第二次失败后 retry 到 attempt=2
    service.mark_running(rid)
    service.mark_failed(rid, error_code="E", error_message="e")
    retried = service.retry_run(rid)
    assert retried["attempt"] == 2

    # 第三次失败后 attempts 用尽 -> retry 非法 -> mark_dead
    service.mark_running(rid)
    service.mark_failed(rid, error_code="E", error_message="e")
    with pytest.raises(InvalidRunTransition):
        service.retry_run(rid)
    dead = service.mark_dead(rid, error_code="E", error_message="exhausted")
    assert dead["status"] == RunStatus.DEAD.value


@pytest.mark.unit
def test_cancel_before_start(service):
    run = service.create_run(query="q", session_id="s1")
    rid = run["id"]
    service.mark_queued(rid)
    cancelled = service.cancel_run(rid)
    assert cancelled["status"] == RunStatus.CANCELLED.value

    # 已 RUNNING 的 run 不能取消（当前为执行前取消语义）
    run2 = service.create_run(query="q2", session_id="s2")
    service.mark_queued(run2["id"])
    service.mark_running(run2["id"])
    with pytest.raises(InvalidRunTransition):
        service.cancel_run(run2["id"])


@pytest.mark.unit
def test_idempotency_key(service):
    first = service.create_run(query="q", session_id="s1", idempotency_key="key-1")
    second = service.create_run(query="different", session_id="s2", idempotency_key="key-1")
    assert first["id"] == second["id"]
    assert second["session_id"] == "s1"


@pytest.mark.unit
def test_require_run_not_found(service):
    with pytest.raises(RunNotFound):
        service.require_run("does-not-exist")


# ---------------------------------------------------------------------------
# 2. worker（executor）行为
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_worker_success(service):
    run = service.create_run(query="你好", session_id="s1", user_id="u1")
    rid = run["id"]
    service.mark_queued(rid)
    runtime = _FakeRuntime(result={"response": "hi", "current_agent": "general_agent"})

    status = await execute_run(rid, service=service, lock_manager=NullThreadLock(), runtime_provider=_provider(runtime))

    assert status == RunStatus.SUCCEEDED.value
    stored = service.get_run(rid)
    assert stored["status"] == RunStatus.SUCCEEDED.value
    assert stored["result"]["response"] == "hi"
    assert runtime.calls[0]["thread_id"] == "s1"
    assert runtime.calls[0]["user_id"] == "u1"


@pytest.mark.unit
async def test_worker_failure(service):
    run = service.create_run(query="q", session_id="s1", max_attempts=1)
    rid = run["id"]
    service.mark_queued(rid)
    runtime = _FakeRuntime(error=RuntimeError("llm down"))

    status = await execute_run(rid, service=service, lock_manager=NullThreadLock(), runtime_provider=_provider(runtime))

    assert status == RunStatus.FAILED.value  # max_attempts<=1：无重试，FAILED 终态
    stored = service.get_run(rid)
    assert stored["status"] == RunStatus.FAILED.value
    assert stored["error_code"] == "RuntimeError"


@pytest.mark.unit
async def test_worker_retry(service):
    run = service.create_run(query="q", session_id="s1", max_attempts=2)
    rid = run["id"]
    service.mark_queued(rid)
    runtime = _FakeRuntime(error=RuntimeError("transient"))
    dispatched: list[str] = []

    async def _dispatcher(run_id: str):
        dispatched.append(run_id)

    # 第一次失败：attempt 0 -> 1，重新入队
    status = await execute_run(
        rid, service=service, lock_manager=NullThreadLock(), runtime_provider=_provider(runtime), dispatcher=_dispatcher
    )
    assert status == RunStatus.QUEUED.value
    stored = service.get_run(rid)
    assert stored["attempt"] == 1
    assert dispatched == [rid]

    # 第二次失败：attempts 用尽 -> DEAD
    status = await execute_run(
        rid, service=service, lock_manager=NullThreadLock(), runtime_provider=_provider(runtime), dispatcher=_dispatcher
    )
    assert status == RunStatus.DEAD.value
    assert service.get_run(rid)["status"] == RunStatus.DEAD.value


@pytest.mark.unit
async def test_worker_skips_cancelled(service):
    run = service.create_run(query="q", session_id="s1")
    rid = run["id"]
    service.mark_queued(rid)
    service.cancel_run(rid)
    runtime = _FakeRuntime()

    status = await execute_run(rid, service=service, lock_manager=NullThreadLock(), runtime_provider=_provider(runtime))

    assert status == RunStatus.CANCELLED.value
    assert runtime.calls == []  # 取消后不得执行
