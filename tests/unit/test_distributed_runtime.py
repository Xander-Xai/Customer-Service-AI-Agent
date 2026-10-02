"""Distributed Runtime Foundation 单元测试。

覆盖：
  - per-thread 锁语义（同 thread 串行 / 不同 thread 并行 / 超时 THREAD_BUSY）；
  - 生产 session + gunicorn 多 worker 一致性 gate（纯函数）；
  - tool 幂等包裹执行（重试不重复副作用）；
  - REST 层 THREAD_BUSY -> 409 错误契约。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.concurrency.distributed_lock import (
    ThreadBusyError,
    reset_api_lock_manager_for_tests,
    thread_lock,
)
from core.config import validate_distributed_runtime_settings
from runtime.side_effects import SideEffectStore, execute_idempotent_operation
from runtime.thread_lock import InMemoryThreadLock
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory

# ---------------------------------------------------------------------------
# Lock semantics
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_lock_singleton():
    reset_api_lock_manager_for_tests()
    yield
    reset_api_lock_manager_for_tests()


@pytest.mark.unit
async def test_same_thread_is_serialized():
    mgr = InMemoryThreadLock()
    order: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with thread_lock("T1", manager=mgr, acquire_timeout=2.0):
            order.append("a-in")
            entered.set()
            await release.wait()
            order.append("a-out")

    async def waiter():
        await entered.wait()
        async with thread_lock("T1", manager=mgr, acquire_timeout=2.0):
            order.append("b-in")

    ta = asyncio.create_task(holder())
    tb = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert order == ["a-in"]  # b 未进入
    release.set()
    await asyncio.gather(ta, tb)
    assert order == ["a-in", "a-out", "b-in"]


@pytest.mark.unit
async def test_different_threads_run_in_parallel():
    mgr = InMemoryThreadLock()
    both = asyncio.Event()
    count = {"n": 0, "max": 0}

    async def worker(thread_id: str):
        async with thread_lock(thread_id, manager=mgr, acquire_timeout=2.0):
            count["n"] += 1
            count["max"] = max(count["max"], count["n"])
            if count["n"] >= 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=2.0)
            count["n"] -= 1

    await asyncio.gather(worker("T1"), worker("T2"))
    assert count["max"] == 2


@pytest.mark.unit
async def test_acquire_timeout_raises_thread_busy():
    mgr = InMemoryThreadLock()
    await mgr.acquire("T1", "holder", ttl_seconds=30)
    with pytest.raises(ThreadBusyError) as ei:
        async with thread_lock("T1", manager=mgr, acquire_timeout=0.2):
            pass
    assert ei.value.code == "THREAD_BUSY"
    assert ei.value.thread_id == "T1"


@pytest.mark.unit
async def test_lock_disabled_is_noop():
    # manager=None 且未启用 -> 直接放行，不阻塞
    async with thread_lock("T1", manager=None) as owner:
        assert owner is None or isinstance(owner, str)


@pytest.mark.unit
async def test_lock_released_after_exception():
    mgr = InMemoryThreadLock()
    with pytest.raises(RuntimeError):
        async with thread_lock("T1", manager=mgr, acquire_timeout=1.0):
            raise RuntimeError("boom")
    # 异常后锁必须释放，可再次获取
    async with thread_lock("T1", manager=mgr, acquire_timeout=1.0):
        pass


# ---------------------------------------------------------------------------
# Production distributed-runtime gates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_production_requires_redis_session():
    problems = validate_distributed_runtime_settings(
        dev_mode=False,
        session_backend="memory",
        checkpoint_backend="postgres",
        gunicorn_workers=1,
        lock_enabled=True,
        lock_backend="redis",
    )
    assert any("SESSION_STORAGE_BACKEND=redis" in p for p in problems)


@pytest.mark.unit
def test_multi_worker_requires_durable_checkpoint_session_and_lock():
    problems = validate_distributed_runtime_settings(
        dev_mode=False,
        session_backend="memory",
        checkpoint_backend="memory",
        gunicorn_workers=4,
        lock_enabled=False,
        lock_backend="memory",
    )
    joined = " | ".join(problems)
    assert "GUNICORN_WORKERS=4" in joined
    assert "LANGGRAPH_CHECKPOINT_BACKEND=postgres" in joined
    assert "SESSION_STORAGE_BACKEND=redis" in joined
    assert "AGENT_RUN_THREAD_LOCK" in joined


@pytest.mark.unit
def test_production_correct_config_passes():
    assert (
        validate_distributed_runtime_settings(
            dev_mode=False,
            session_backend="redis",
            checkpoint_backend="postgres",
            gunicorn_workers=4,
            lock_enabled=True,
            lock_backend="redis",
        )
        == []
    )


@pytest.mark.unit
def test_dev_mode_has_no_constraints():
    assert (
        validate_distributed_runtime_settings(
            dev_mode=True,
            session_backend="memory",
            checkpoint_backend="memory",
            gunicorn_workers=4,
            lock_enabled=False,
            lock_backend="memory",
        )
        == []
    )


# ---------------------------------------------------------------------------
# Tool idempotency
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_tool_side_effect_executes_once_across_retry():
    session_factory, engine, path = make_sqlite_session_factory()
    store = SideEffectStore(session_factory=session_factory)
    calls = {"n": 0}

    def side_effect():
        calls["n"] += 1
        return {"refund_id": "R-1"}

    try:
        first = await execute_idempotent_operation(
            tool_name="refund",
            operation_key="refund:order-1:req-9",
            run_id="run-1",
            thread_id="T1",
            arguments={"order_id": "order-1", "amount": 10},
            operation=side_effect,
            store=store,
        )
        # 模拟 graph 后续失败 -> 整个 run 重试（新 run_id，同一业务操作键）
        second = await execute_idempotent_operation(
            tool_name="refund",
            operation_key="refund:order-1:req-9",
            run_id="run-2",
            thread_id="T1",
            arguments={"order_id": "order-1", "amount": 10},
            operation=side_effect,
            store=store,
        )
    finally:
        dispose(engine, path)

    assert calls["n"] == 1  # 副作用函数只执行一次
    assert first == second == {"refund_id": "R-1"}


@pytest.mark.unit
async def test_tool_idempotency_fingerprint_conflict():
    from runtime.errors import PermanentError

    session_factory, engine, path = make_sqlite_session_factory()
    store = SideEffectStore(session_factory=session_factory)

    async def op():
        return {"ok": True}

    try:
        await execute_idempotent_operation(
            tool_name="refund",
            operation_key="refund:order-1:req-9",
            run_id="run-1",
            thread_id="T1",
            arguments={"amount": 10},
            operation=op,
            store=store,
        )
        with pytest.raises(PermanentError):
            await execute_idempotent_operation(
                tool_name="refund",
                operation_key="refund:order-1:req-9",
                run_id="run-2",
                thread_id="T1",
                arguments={"amount": 999},  # 同键不同参数 -> 冲突
                operation=op,
                store=store,
            )
    finally:
        dispose(engine, path)


# ---------------------------------------------------------------------------
# REST THREAD_BUSY -> 409
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rest_chat_maps_thread_busy_to_409():
    from api.routes.chat import router

    async def busy_run_graph(sid, query, **kwargs):
        raise ThreadBusyError(sid, 0.1)

    app = FastAPI()
    app.state.run_graph = busy_run_graph
    app.state.session_manager = None
    app.state.dev_mode = True
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post("/api/chat", json={"query": "你好", "session_id": "T-busy"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "THREAD_BUSY"
    assert body["code"] == "THREAD_BUSY"
