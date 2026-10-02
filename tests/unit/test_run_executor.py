"""执行器可靠性测试（Test B–G，确定性、无外部依赖）。

覆盖：
  B. 同 thread 互斥（critical section max concurrency == 1）
  C. 不同 thread 并行
  D. 重复 run_id 只执行一次（application-level 幂等）
  E. transient retry -> success + retry metric
  F. retry 用尽 -> DEAD_LETTER + 可查询 DLQ evidence
  G. permanent failure -> FAILED，不进入 retry loop
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from runtime.executor import RetryPublicationError, execute_run
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.statuses import RunStatus
from runtime.thread_lock import InMemoryThreadLock
from tests.unit.runtime_helpers import (
    FakeRuntime,
    RecordingDispatcher,
    counter_value,
    dispose,
    make_sqlite_session_factory,
    runtime_provider,
)


class FailingDispatcher:
    """投递器：模拟 Redis/Celery broker 抖动（延迟重试消息发不出去）。"""

    def __init__(self):
        self.calls: list[tuple[str, float | None]] = []

    async def __call__(self, run_id: str, countdown: float | None = None):
        self.calls.append((run_id, countdown))
        raise ConnectionError("broker down")


@pytest.fixture
def env():
    session_factory, engine, path = make_sqlite_session_factory()
    repo = AgentRunRepository(session_factory=session_factory)
    service = RunService(repository=repo)
    yield service
    dispose(engine, path)


async def _run(service, run_id, runtime, dispatcher, lock):
    return await execute_run(
        run_id,
        service=service,
        runtime_provider=runtime_provider(runtime),
        dispatcher=dispatcher,
        lock_manager=lock,
        worker_id="w-test",
    )


# ---------------------------------------------------------------------------
# Test D — 重复 run_id 只执行一次
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_duplicate_run_id_executes_graph_once(env):
    service = env
    run = service.create_run(query="q", session_id="T-dup")
    runtime = FakeRuntime(lambda **kw: {"response": "ok"})
    dispatcher = RecordingDispatcher()
    lock = InMemoryThreadLock()

    first = await _run(service, run["id"], runtime, dispatcher, lock)
    assert first == RunStatus.SUCCEEDED.value

    # 第二次投递：终态 no-op，不再执行 graph
    second = await _run(service, run["id"], runtime, dispatcher, lock)
    assert second == RunStatus.SUCCEEDED.value
    assert len(runtime.calls) == 1
    assert service.get_run(run["id"])["result"] == {"response": "ok"}


# ---------------------------------------------------------------------------
# Test G — permanent failure，不重试
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_permanent_failure_no_retry(env):
    service = env
    run = service.create_run(query="q", session_id="T-perm")

    def boom(**kwargs):
        raise ValueError("invalid arguments")

    runtime = FakeRuntime(boom)
    dispatcher = RecordingDispatcher()
    lock = InMemoryThreadLock()

    status = await _run(service, run["id"], runtime, dispatcher, lock)
    assert status == RunStatus.FAILED.value
    stored = service.get_run(run["id"])
    assert stored["attempt"] == 1
    assert stored["error_type"] == "permanent"
    assert dispatcher.calls == []
    assert service.get_dead_letter(run["id"]) is None


# ---------------------------------------------------------------------------
# Test E — transient retry -> success
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_transient_retry_then_success(env):
    service = env
    run = service.create_run(query="q", session_id="T-retry", max_attempts=3)
    state = {"calls": 0}

    def flaky(**kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise TimeoutError("provider timeout")
        return {"response": "ok"}

    runtime = FakeRuntime(flaky)
    dispatcher = RecordingDispatcher()
    lock = InMemoryThreadLock()
    retries_before = counter_value("agent_run_retry_total")

    first = await _run(service, run["id"], runtime, dispatcher, lock)
    assert first == RunStatus.RETRYING.value
    stored = service.get_run(run["id"])
    assert stored["attempt"] == 1
    assert stored["next_retry_at"] is not None
    assert len(dispatcher.calls) == 1  # 已重新调度

    second = await _run(service, run["id"], runtime, dispatcher, lock)
    assert second == RunStatus.SUCCEEDED.value
    assert service.get_run(run["id"])["attempt"] == 2
    # retry metric 仅在 prometheus_client 可用时存在（CI 可能未安装）
    from core.monitoring import PROMETHEUS_BUSINESS_ENABLED

    if PROMETHEUS_BUSINESS_ENABLED:
        assert counter_value("agent_run_retry_total") >= retries_before + 1


# ---------------------------------------------------------------------------
# Test F — retry 用尽 -> DEAD_LETTER + DLQ
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_retry_exhausted_dead_letter(env):
    service = env
    run = service.create_run(query="q", session_id="T-dlq", max_attempts=3)

    def always_timeout(**kwargs):
        raise TimeoutError("provider timeout")

    runtime = FakeRuntime(always_timeout)
    dispatcher = RecordingDispatcher()
    lock = InMemoryThreadLock()

    statuses = []
    for _ in range(3):
        statuses.append(await _run(service, run["id"], runtime, dispatcher, lock))

    assert statuses == [
        RunStatus.RETRYING.value,
        RunStatus.RETRYING.value,
        RunStatus.DEAD_LETTER.value,
    ]
    stored = service.get_run(run["id"])
    assert stored["status"] == RunStatus.DEAD_LETTER.value
    assert stored["attempt"] == 3

    record = service.get_dead_letter(run["id"])
    assert record is not None
    assert record["attempt_count"] == 3
    assert record["max_attempts"] == 3
    assert record["error_type"] == "timeout"
    assert "timeout" in (record["error_message"] or "").lower()
    assert record["entered_at"] is not None

    # 第 4 次投递仍是终态 no-op
    assert await _run(service, run["id"], runtime, dispatcher, lock) == "DEAD_LETTER"
    assert len(runtime.calls) == 3


# ---------------------------------------------------------------------------
# Test B — 同 thread 互斥
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_same_thread_mutual_exclusion(env):
    service = env
    lock = InMemoryThreadLock()
    dispatcher = RecordingDispatcher()
    entered = asyncio.Event()
    release = asyncio.Event()
    concurrency = {"current": 0, "max": 0}

    async def slow(**kwargs):
        concurrency["current"] += 1
        concurrency["max"] = max(concurrency["max"], concurrency["current"])
        entered.set()
        await release.wait()
        concurrency["current"] -= 1
        return {"response": "ok"}

    runtime = FakeRuntime(slow)
    run1 = service.create_run(query="a", session_id="T-shared")
    run2 = service.create_run(query="b", session_id="T-shared")

    task1 = asyncio.create_task(_run(service, run1["id"], runtime, dispatcher, lock))
    await asyncio.wait_for(entered.wait(), timeout=3)

    # run2 在同一 thread 上并发投递 -> 不得进入 critical section
    second = await _run(service, run2["id"], runtime, dispatcher, lock)
    assert second in (RunStatus.QUEUED.value, RunStatus.RETRYING.value)
    assert concurrency["max"] == 1
    assert len(runtime.calls) == 1

    release.set()
    first = await asyncio.wait_for(task1, timeout=3)
    assert first == RunStatus.SUCCEEDED.value

    # 锁释放后，run2 可正常执行
    third = await _run(service, run2["id"], runtime, dispatcher, lock)
    assert third == RunStatus.SUCCEEDED.value
    assert concurrency["max"] == 1
    assert len(runtime.calls) == 2


# ---------------------------------------------------------------------------
# Test C — 不同 thread 并行
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_different_threads_run_in_parallel(env):
    service = env
    lock = InMemoryThreadLock()
    dispatcher = RecordingDispatcher()
    both = asyncio.Event()
    count = {"entered": 0, "max": 0}

    async def rendezvous(**kwargs):
        count["entered"] += 1
        count["max"] = max(count["max"], count["entered"])
        if count["entered"] >= 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=3)
        count["entered"] -= 1
        return {"response": "ok"}

    runtime = FakeRuntime(rendezvous)
    run1 = service.create_run(query="a", session_id="T-par-1")
    run2 = service.create_run(query="b", session_id="T-par-2")

    results = await asyncio.gather(
        _run(service, run1["id"], runtime, dispatcher, lock),
        _run(service, run2["id"], runtime, dispatcher, lock),
    )
    assert results == [RunStatus.SUCCEEDED.value, RunStatus.SUCCEEDED.value]
    assert count["max"] == 2  # 确实并发，而不是全局大锁


# ---------------------------------------------------------------------------
# PR #27-B — 重试投递失败绝不能静默 ACK
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_dispatcher_wraps_failure_as_retry_publication_error():
    from runtime.executor import _call_dispatcher

    async def boom(run_id, countdown=None):
        raise ConnectionError("redis down")

    with pytest.raises(RetryPublicationError):
        await _call_dispatcher(boom, "run-x", 5.0)

    # 正常投递不抛
    calls: list[str] = []

    async def ok(run_id, countdown=None):
        calls.append(run_id)

    await _call_dispatcher(ok, "run-y", 5.0)
    assert calls == ["run-y"]


@pytest.mark.unit
async def test_retry_publication_failure_escapes_instead_of_silent_ack(env):
    """run 进入 RETRYING 但重试消息发不出去 -> 异常必须逃逸（Celery 不 ACK）。"""
    service = env
    run = service.create_run(query="q", session_id="T-pubfail", max_attempts=3)

    def always_timeout(**kwargs):
        raise TimeoutError("provider timeout")

    runtime = FakeRuntime(always_timeout)
    dispatcher = FailingDispatcher()
    lock = InMemoryThreadLock()

    with pytest.raises(RetryPublicationError):
        await _run(service, run["id"], runtime, dispatcher, lock)

    # run 停在 RETRYING（等待 broker redelivery / reconciler），而不是被 ACK 后遗忘
    stored = service.get_run(run["id"])
    assert stored["status"] == RunStatus.RETRYING.value
    assert stored["next_retry_at"] is not None
    assert len(dispatcher.calls) == 1


@pytest.mark.unit
async def test_reconciler_redispatches_retrying_run_lost_by_broker(env):
    """durable recovery：投递彻底丢失后，扫描器重新投递 RETRYING/QUEUED 的 run。"""
    from runtime.retry import reconcile_stuck_runs

    service = env
    run = service.create_run(query="q", session_id="T-reconcile", max_attempts=3)
    service.mark_running(run["id"], worker_id="w1", lease_seconds=60)
    service.mark_retrying(
        run["id"], delay_seconds=0.0, error_type="timeout", error_message="timeout"
    )

    dispatcher = RecordingDispatcher()
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    dispatched = await reconcile_stuck_runs(service, dispatcher=dispatcher, now=later)

    assert run["id"] in dispatched
    assert [rid for rid, _ in dispatcher.calls] == [run["id"]]
    # 重新投递后仍可由 worker 领取
    again = service.mark_running(run["id"], worker_id="w2", lease_seconds=60)
    assert again is not None
    assert again["attempt"] == 2
