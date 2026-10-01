"""Run 可靠性测试：thread 串行、幂等、retry、DEAD、副作用去重。

覆盖任务指定用例：
  same_thread_serial_execution / different_thread_parallel_execution /
  duplicate_celery_delivery_does_not_repeat_success /
  lock_owner_cannot_delete_other_owner_lock / lock_expiration_recovery /
  transient_error_retries / permanent_error_no_retry / max_retry_becomes_dead /
  side_effect_not_repeated_after_worker_replay

本地单元测试（独立 SQLite + fake runtime + in-memory lock），不依赖 Redis/Celery。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base
from runtime.errors import PermanentError, TransientError
from runtime.executor import execute_run
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService, build_idempotency_scope
from runtime.side_effects import SideEffectStore
from runtime.statuses import RunStatus
from runtime.thread_lock import InMemoryThreadLock
from tools.tool_registry import ToolRegistry


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    service = RunService(AgentRunRepository(session_factory))
    return service, session_factory


def _queued(service, *, thread="t1", session="s1", max_attempts=3):
    run = service.create_run(
        query="q", session_id=session, thread_id=thread, max_attempts=max_attempts
    )
    service.mark_queued(run["id"])
    return run["id"]


class _RecordingDispatcher:
    def __init__(self):
        self.calls: list[tuple[str, float]] = []

    async def __call__(self, run_id, countdown=None):
        self.calls.append((run_id, countdown or 0))


# ---------------------------------------------------------------------------
# 1. Thread lock 语义
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_lock_owner_cannot_delete_other_owner_lock():
    lock = InMemoryThreadLock()
    assert await lock.acquire("thread-A", "owner-1", 60) is True
    # 非 owner 不能释放
    assert await lock.release("thread-A", "owner-2") is False
    assert await lock.is_locked("thread-A") is True
    # owner 才能释放
    assert await lock.release("thread-A", "owner-1") is True
    assert await lock.is_locked("thread-A") is False


@pytest.mark.unit
async def test_lock_expiration_recovery():
    lock = InMemoryThreadLock()
    assert await lock.acquire("thread-A", "owner-1", 60) is True
    assert await lock.acquire("thread-A", "owner-2", 60) is False
    await lock.expire("thread-A")  # 模拟 lease 过期
    assert await lock.acquire("thread-A", "owner-2", 60) is True


# ---------------------------------------------------------------------------
# 2. 同 thread 串行 / 不同 thread 并行
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_same_thread_serial_execution(env):
    service, _ = env
    rid1 = _queued(service, thread="T", session="s1")
    rid2 = _queued(service, thread="T", session="s2")

    entered = asyncio.Event()
    release = asyncio.Event()

    async def runtime_provider():
        class RT:
            async def run(self, *, thread_id, query, user_id=None):
                entered.set()
                await release.wait()
                return {"response": "ok"}

        return RT()

    lock = InMemoryThreadLock()
    dispatcher = _RecordingDispatcher()
    task1 = asyncio.create_task(
        execute_run(
            rid1,
            service=service,
            runtime_provider=runtime_provider,
            lock_manager=lock,
            dispatcher=dispatcher,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)

    # 同 thread 第二个 run 被占用 -> 保持 QUEUED，不执行
    status2 = await execute_run(
        rid2,
        service=service,
        runtime_provider=runtime_provider,
        lock_manager=lock,
        dispatcher=dispatcher,
    )
    assert status2 == RunStatus.QUEUED.value
    assert service.get_run(rid2)["status"] == RunStatus.QUEUED.value
    assert dispatcher.calls and dispatcher.calls[-1][1] > 0  # 延迟重调度

    release.set()
    assert await asyncio.wait_for(task1, timeout=5) == RunStatus.SUCCEEDED.value
    assert service.get_run(rid1)["status"] == RunStatus.SUCCEEDED.value


@pytest.mark.unit
async def test_different_thread_parallel_execution(env):
    service, _ = env
    rid1 = _queued(service, thread="A", session="sa")
    rid2 = _queued(service, thread="B", session="sb")

    both_entered = asyncio.Event()
    release = asyncio.Event()
    state = {"count": 0}

    async def runtime_provider():
        class RT:
            async def run(self, *, thread_id, query, user_id=None):
                state["count"] += 1
                if state["count"] == 2:
                    both_entered.set()
                await release.wait()
                return {"response": thread_id}

        return RT()

    lock = InMemoryThreadLock()
    t1 = asyncio.create_task(
        execute_run(rid1, service=service, runtime_provider=runtime_provider, lock_manager=lock)
    )
    t2 = asyncio.create_task(
        execute_run(rid2, service=service, runtime_provider=runtime_provider, lock_manager=lock)
    )
    await asyncio.wait_for(both_entered.wait(), timeout=5)  # 两个 thread 可并行
    release.set()
    results = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5)
    assert results == [RunStatus.SUCCEEDED.value, RunStatus.SUCCEEDED.value]


# ---------------------------------------------------------------------------
# 3. 重复投递幂等
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_duplicate_celery_delivery_does_not_repeat_success(env):
    service, _ = env
    rid = _queued(service)
    calls = {"n": 0}

    async def runtime_provider():
        class RT:
            async def run(self, *, thread_id, query, user_id=None):
                calls["n"] += 1
                return {"response": "ok"}

        return RT()

    lock = InMemoryThreadLock()
    first = await execute_run(
        rid, service=service, runtime_provider=runtime_provider, lock_manager=lock
    )
    second = await execute_run(
        rid, service=service, runtime_provider=runtime_provider, lock_manager=lock
    )
    assert first == second == RunStatus.SUCCEEDED.value
    assert calls["n"] == 1  # 重复投递不重复执行


# ---------------------------------------------------------------------------
# 4. retry / permanent / dead
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_transient_error_retries(env):
    service, _ = env
    rid = _queued(service, max_attempts=3)

    async def runtime_provider():
        class RT:
            async def run(self, *, thread_id, query, user_id=None):
                raise TransientError("429 rate limited")

        return RT()

    dispatcher = _RecordingDispatcher()
    status = await execute_run(
        rid,
        service=service,
        runtime_provider=runtime_provider,
        lock_manager=InMemoryThreadLock(),
        dispatcher=dispatcher,
    )
    assert status == RunStatus.QUEUED.value
    run = service.get_run(rid)
    assert run["attempt"] == 1
    assert run["error_type"] == "transient"
    assert run["next_retry_at"] is not None
    assert dispatcher.calls and dispatcher.calls[0][1] > 0  # 指数退避 countdown


@pytest.mark.unit
async def test_permanent_error_no_retry(env):
    service, _ = env
    rid = _queued(service, max_attempts=3)

    async def runtime_provider():
        class RT:
            async def run(self, *, thread_id, query, user_id=None):
                raise PermanentError("invalid argument")

        return RT()

    dispatcher = _RecordingDispatcher()
    status = await execute_run(
        rid,
        service=service,
        runtime_provider=runtime_provider,
        lock_manager=InMemoryThreadLock(),
        dispatcher=dispatcher,
    )
    assert status == RunStatus.DEAD.value
    run = service.get_run(rid)
    assert run["status"] == RunStatus.DEAD.value
    assert run["error_type"] == "permanent"
    assert dispatcher.calls == []  # permanent 不 retry


@pytest.mark.unit
async def test_max_retry_becomes_dead(env):
    service, _ = env
    rid = _queued(service, max_attempts=2)
    calls = {"n": 0}

    async def runtime_provider():
        class RT:
            async def run(self, *, thread_id, query, user_id=None):
                calls["n"] += 1
                raise TransientError("boom")

        return RT()

    dispatcher = _RecordingDispatcher()
    lock = InMemoryThreadLock()
    first = await execute_run(
        rid, service=service, runtime_provider=runtime_provider, lock_manager=lock, dispatcher=dispatcher
    )
    assert first == RunStatus.QUEUED.value
    second = await execute_run(
        rid, service=service, runtime_provider=runtime_provider, lock_manager=lock, dispatcher=dispatcher
    )
    assert second == RunStatus.DEAD.value
    assert calls["n"] == 2
    assert service.get_run(rid)["status"] == RunStatus.DEAD.value


# ---------------------------------------------------------------------------
# 5. Tool 副作用幂等（worker 崩溃重投）
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_side_effect_not_repeated_after_worker_replay(env):
    """退款成功 -> run commit 前 worker crash -> 重投 -> 不重复退款。"""
    from runtime.context import reset_run_context, set_run_context

    service, session_factory = env
    rid = _queued(service, thread="T-refund", max_attempts=3)

    store = SideEffectStore(session_factory)
    registry = ToolRegistry(side_effect_store=store)
    refund_calls = {"n": 0}

    async def refund(args):
        refund_calls["n"] += 1
        return {"refund_id": "R-1", "order": args["order"]}

    registry.register(
        name="refund",
        description="refund order",
        parameters={},
        handler=refund,
        side_effect=True,
        operation_key=lambda a: f"refund:{a['order']}",
    )

    attempts = {"n": 0}

    async def runtime_provider():
        class RT:
            async def run(self, *, thread_id, query, user_id=None):
                attempts["n"] += 1
                tokens = set_run_context(rid, thread_id)
                try:
                    result = await registry.execute_raw("refund", {"order": "ORD-9"})
                finally:
                    reset_run_context(tokens)
                if attempts["n"] == 1:
                    # 副作用已成功，但 run/checkpoint commit 前崩溃
                    raise TransientError("worker crashed after side effect")
                return {"response": "done", "refund": result}

        return RT()

    lock = InMemoryThreadLock()
    dispatcher = _RecordingDispatcher()
    first = await execute_run(
        rid, service=service, runtime_provider=runtime_provider, lock_manager=lock, dispatcher=dispatcher
    )
    assert first == RunStatus.QUEUED.value  # 崩溃后进入 retry
    assert refund_calls["n"] == 1

    # 重投：同一 run 再次执行，副作用不得重复
    second = await execute_run(
        rid, service=service, runtime_provider=runtime_provider, lock_manager=lock, dispatcher=dispatcher
    )
    assert second == RunStatus.SUCCEEDED.value
    assert refund_calls["n"] == 1  # 关键：退款只发生一次
    assert service.get_run(rid)["status"] == RunStatus.SUCCEEDED.value


# ---------------------------------------------------------------------------
# 6. idempotency scope
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_idempotency_scope_is_user_and_endpoint_bound():
    a = build_idempotency_scope("u1", "POST:/api/runs", "key")
    b = build_idempotency_scope("u2", "POST:/api/runs", "key")
    c = build_idempotency_scope("u1", "POST:/other", "key")
    assert a != b != c != a
    assert build_idempotency_scope(None, "POST:/api/runs", "k").startswith("anon:")
