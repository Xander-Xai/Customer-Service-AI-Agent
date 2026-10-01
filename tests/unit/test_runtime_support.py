"""Thread 锁与副作用幂等 ledger 单元测试（内存后端，确定性）。"""

from __future__ import annotations

import pytest

from runtime.side_effects import (
    CLAIM_CONFLICT,
    CLAIM_EXECUTE,
    CLAIM_SUCCEEDED,
    SideEffectStore,
    build_tool_idempotency_key,
    request_fingerprint,
)
from runtime.thread_lock import InMemoryThreadLock
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory

# ---------------------------------------------------------------------------
# Thread lock
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_lock_owner_semantics_and_ttl():
    lock = InMemoryThreadLock()
    assert await lock.acquire("T1", "owner-a", ttl_seconds=30) is True
    # 同一 thread 第二个 owner 拿不到
    assert await lock.acquire("T1", "owner-b", ttl_seconds=30) is False
    # 不同 thread 可并发
    assert await lock.acquire("T2", "owner-b", ttl_seconds=30) is True
    # 非 owner 不能释放
    assert await lock.release("T1", "owner-b") is False
    assert await lock.is_locked("T1") is True
    # owner 释放成功
    assert await lock.release("T1", "owner-a") is True
    assert await lock.is_locked("T1") is False
    # 释放后可重新获取
    assert await lock.acquire("T1", "owner-c", ttl_seconds=30) is True


@pytest.mark.unit
async def test_lock_ttl_expiry_allows_takeover():
    lock = InMemoryThreadLock()
    await lock.acquire("T1", "owner-a", ttl_seconds=30)
    await lock.expire("T1")  # 模拟持有者崩溃后 lease 过期
    assert await lock.acquire("T1", "owner-b", ttl_seconds=30) is True


@pytest.mark.unit
async def test_lock_refresh_only_by_owner():
    lock = InMemoryThreadLock()
    await lock.acquire("T1", "owner-a", ttl_seconds=30)
    assert await lock.refresh("T1", "owner-b", ttl_seconds=30) is False
    assert await lock.refresh("T1", "owner-a", ttl_seconds=30) is True


# ---------------------------------------------------------------------------
# Side-effect ledger
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    session_factory, engine, path = make_sqlite_session_factory()
    yield SideEffectStore(session_factory=session_factory)
    dispose(engine, path)


@pytest.mark.unit
def test_tool_idempotency_key_convention():
    assert build_tool_idempotency_key("run-1", "call-9") == "run-1:call-9"


@pytest.mark.unit
def test_side_effect_claim_deduplicates_after_success(store):
    op_key = build_tool_idempotency_key("run-1", "call-1")
    fp = request_fingerprint({"order": "A", "amount": 10})

    first = store.claim(
        tool_name="refund",
        operation_key=op_key,
        run_id="run-1",
        thread_id="T1",
        fingerprint=fp,
    )
    assert first.state == CLAIM_EXECUTE
    store.mark_succeeded("refund", op_key, {"refund_id": "R1"})

    # 崩溃恢复/重投递：不得再次执行，返回已存结果
    second = store.claim(
        tool_name="refund",
        operation_key=op_key,
        run_id="run-1",
        thread_id="T1",
        fingerprint=fp,
    )
    assert second.state == CLAIM_SUCCEEDED
    assert second.result == {"refund_id": "R1"}


@pytest.mark.unit
def test_side_effect_fingerprint_mismatch_conflicts(store):
    op_key = build_tool_idempotency_key("run-2", "call-1")
    store.claim(
        tool_name="refund",
        operation_key=op_key,
        run_id="run-2",
        thread_id="T1",
        fingerprint=request_fingerprint({"amount": 10}),
    )
    store.mark_succeeded("refund", op_key, {"refund_id": "R2"})

    conflict = store.claim(
        tool_name="refund",
        operation_key=op_key,
        run_id="run-2",
        thread_id="T1",
        fingerprint=request_fingerprint({"amount": 999}),
    )
    assert conflict.state == CLAIM_CONFLICT
