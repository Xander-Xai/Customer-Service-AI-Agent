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
    # 必须携带 claim 返回的 owner：结果提交是带归属谓词的条件 UPDATE，
    # 没有 owner 等于放弃生产级写入权限检查（fail-closed）。
    assert store.mark_succeeded("refund", op_key, {"refund_id": "R1"}, owner=first.owner) is True

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
    fp = request_fingerprint({"amount": 10})
    first = store.claim(
        tool_name="refund",
        operation_key=op_key,
        run_id="run-2",
        thread_id="T1",
        fingerprint=fp,
    )
    assert first.state == CLAIM_EXECUTE
    assert store.mark_succeeded("refund", op_key, {"refund_id": "R2"}, owner=first.owner) is True

    conflict = store.claim(
        tool_name="refund",
        operation_key=op_key,
        run_id="run-2",
        thread_id="T1",
        fingerprint=request_fingerprint({"amount": 999}),
    )
    assert conflict.state == CLAIM_CONFLICT


@pytest.mark.unit
def test_result_commit_requires_an_owner(store):
    """owner 缺失必须 fail-closed，而不是悄悄退化成无条件写入。

    历史缺陷：``owner`` 是可选参数，传 ``None`` 时归属谓词被整体跳过，于是
    任何调用方都能改写别人的副作用结果（真实 PG 并发复现见
    ``tests/integration/runtime/test_side_effect_commit_fencing_pg.py``）。
    """
    from runtime.errors import PermanentError

    op_key = build_tool_idempotency_key("run-3", "call-1")
    claim = store.claim(
        tool_name="refund",
        operation_key=op_key,
        run_id="run-3",
        thread_id="T1",
        fingerprint=request_fingerprint({"amount": 10}),
    )
    assert claim.state == CLAIM_EXECUTE

    with pytest.raises(PermanentError):
        store.mark_succeeded("refund", op_key, {"refund_id": "R3"}, owner=None)
    with pytest.raises(PermanentError):
        store.mark_failed("refund", op_key, error_type="E", error_message="m", owner=None)

    # 未被写坏：认领仍是 PENDING，当前 owner 仍能正常提交。
    assert store.mark_succeeded("refund", op_key, {"refund_id": "R3"}, owner=claim.owner) is True
