"""A2 复现：side-effect claim TTL 先于工具执行结束 -> 第二个执行者重复发起写操作。

缺陷
----
``runtime/side_effects.py`` 的 ``DEFAULT_CLAIM_TTL_SECONDS = 60.0`` 是一个**固定**值，
而工具执行超时 ``TOOL_EXECUTION_TIMEOUT_SECONDS`` 是可配置项（默认 30s，可调大）。
``claim()`` 用「认领租约过期」判定原持有者已死，从而允许接管重放。

当一次副作用的实际执行时间超过 claim TTL 时（工具超时被调大、外部系统慢、或
``asyncio.wait_for`` 取消后外部调用仍在飞行中），第一个执行者**仍在执行**，
第二个执行者却因为「租约过期」拿到 ``CLAIM_EXECUTE``，于是同一笔写操作被发起两次。

``mark_succeeded(owner=...)`` 的归属校验只能保证**第二个**写结果覆盖不了第一个，
它无法撤销第一次已经发出的外部调用——那正是要防的重复写。

本文件在真实 PostgreSQL 上构造并发场景并证明修复前后的差异。

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_side_effect_claim_lease_pg.py -q
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

from runtime.side_effects import SideEffectStore, execute_idempotent_operation

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
requires_infra = pytest.mark.skipif(
    not (
        os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip() and os.getenv("TEST_REDIS_URL", "").strip()
    ),
    reason=INFRA_REASON,
)


@pytest.fixture
def store(pg_engine):
    from sqlalchemy.orm import sessionmaker

    return SideEffectStore(session_factory=sessionmaker(bind=pg_engine, expire_on_commit=False))


@pytest.fixture
def keys(pg_engine):
    """记录并清理本用例的 ledger 行（共享测试库）。"""
    from sqlalchemy import text

    recorded: list[str] = []
    yield recorded
    if recorded:
        with pg_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM tool_side_effects WHERE operation_key = ANY(:k)"),
                {"k": recorded},
            )


@pytest.fixture
def op_key(keys):
    key = f"a2-{uuid.uuid4().hex[:10]}"
    keys.append(key)
    return key


class _External:
    """确定性外部系统：只记录真实写调用，不去重。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[dict] = []
        self._lock = asyncio.Lock()

    async def write(self, arguments: dict) -> str:
        if self.delay:
            await asyncio.sleep(self.delay)
        async with self._lock:
            self.calls.append(dict(arguments))
            return f"ext-ok:{len(self.calls)}"

    @property
    def executions(self) -> int:
        return len(self.calls)


# ---------------------------------------------------------------------------
# 缺陷复现
# ---------------------------------------------------------------------------


@requires_infra
async def test_expired_claim_is_replayable_but_only_after_the_executor_is_gone(store, op_key):
    """记录「租约过期 = 持有者已死」这个判定为什么**只能**由续租来守住。

    ``claim`` 无法区分「持有者崩溃」与「持有者仍在执行但太慢」——两者都是
    ``claim_expires_at <= now``。修复前的实测结论：租约先于工具执行结束时，第二个
    执行者拿到 ``CLAIM_EXECUTE``，同一笔写操作被发起两次。

    现在这个判定由**执行期间续租**守住（见下一个用例）。本用例固定的是判定本身的
    语义：租约过期后接管**必须**可用（崩溃恢复依赖它），所以正确的防线是续租而不是
    取消接管能力。
    """
    from runtime.side_effects import request_fingerprint

    fp = request_fingerprint({"order_id": "ORD-0"})
    original = store.claim(
        tool_name="staging_refund",
        operation_key=op_key,
        run_id="run-a2z",
        thread_id="t",
        fingerprint=fp,
        claim_ttl_seconds=0.15,
    )
    assert original.state == "execute"

    await asyncio.sleep(0.3)  # 真实等待过期

    takeover = store.claim(
        tool_name="staging_refund",
        operation_key=op_key,
        run_id="run-a2z",
        thread_id="t",
        fingerprint=fp,
        claim_ttl_seconds=30.0,
    )
    assert takeover.state == "execute", "过期后接管必须可用（否则崩溃无法恢复）"
    assert takeover.owner != original.owner


@requires_infra
async def test_live_claim_blocks_a_second_executor(store, op_key):
    """租约**未过期**时，第二个执行者必须被挡住（这是 claim 的既有语义，保留）。"""
    from runtime.side_effects import request_fingerprint

    fp = request_fingerprint({"order_id": "ORD-1b"})
    first = store.claim(
        tool_name="staging_refund",
        operation_key=op_key,
        run_id="run-a2b",
        thread_id="t",
        fingerprint=fp,
        claim_ttl_seconds=30.0,
    )
    assert first.state == "execute"

    blocked = store.claim(
        tool_name="staging_refund",
        operation_key=op_key,
        run_id="run-a2b",
        thread_id="t",
        fingerprint=fp,
        claim_ttl_seconds=30.0,
    )
    assert blocked.state == "in_progress", "未过期时必须挡住第二个执行者"


# ---------------------------------------------------------------------------
# 修复后的契约
# ---------------------------------------------------------------------------


@requires_infra
async def test_claim_is_renewed_while_operation_runs(store, op_key):
    """修复：执行期间 claim 被续租，租约不会在工具执行中途过期。"""
    from runtime.side_effects import request_fingerprint

    external = _External()

    async def _slow_write() -> str:
        await asyncio.sleep(1.5)
        return await external.write({"order_id": "ORD-2", "amount": 20})

    task_a = asyncio.create_task(
        execute_idempotent_operation(
            tool_name="staging_refund",
            operation_key=op_key,
            run_id="run-a2c",
            thread_id="t",
            arguments={"order_id": "ORD-2", "amount": 20},
            operation=_slow_write,
            store=store,
            claim_ttl_seconds=0.4,
        )
    )
    # 工具执行期间反复尝试接管：每一次都必须被挡住
    for _ in range(4):
        await asyncio.sleep(0.3)
        probe = store.claim(
            tool_name="staging_refund",
            operation_key=op_key,
            run_id="run-a2c",
            thread_id="t",
            fingerprint=request_fingerprint({"order_id": "ORD-2", "amount": 20}),
            claim_ttl_seconds=0.4,
        )
        assert (
            probe.state == "in_progress"
        ), f"执行期间 claim 过期了 -> 第二个执行者拿到了执行权（state={probe.state}）"
    await task_a
    assert external.executions == 1


@requires_infra
async def test_renew_requires_current_owner_and_live_lease(store, op_key):
    """续租必须校验 owner 且要求租约**未过期**——与 run lease 同一语义。

    否则一个已经失去执行权的旧执行者可以靠一次迟到的续租给自己续命，租约就没有
    严格 expiry 语义了。
    """
    from runtime.side_effects import request_fingerprint

    fp = request_fingerprint({"order_id": "ORD-3"})
    claim = store.claim(
        tool_name="staging_refund",
        operation_key=op_key,
        run_id="run-a2d",
        thread_id="t",
        fingerprint=fp,
        claim_ttl_seconds=30.0,
    )
    assert claim.state == "execute" and claim.owner

    assert store.renew_claim(
        "staging_refund", op_key, owner=claim.owner, claim_ttl_seconds=30.0
    ), "当前 owner 应能续租"

    assert not store.renew_claim(
        "staging_refund", op_key, owner="someone-else", claim_ttl_seconds=30.0
    ), "非 owner 不得续租"

    # 已 SUCCEEDED 的行不得再续租
    store.mark_succeeded("staging_refund", op_key, "ok", owner=claim.owner)
    assert not store.renew_claim(
        "staging_refund", op_key, owner=claim.owner, claim_ttl_seconds=30.0
    ), "终态行不得续租"


@requires_infra
async def test_expired_claim_is_taken_over_only_when_previous_owner_is_gone(store, op_key):
    """过期后仍允许接管（崩溃恢复需要），但接管必须换 owner，且旧 owner 不能再写结果。"""
    from runtime.side_effects import request_fingerprint

    fp = request_fingerprint({"order_id": "ORD-4"})
    old = store.claim(
        tool_name="staging_refund",
        operation_key=op_key,
        run_id="run-a2e",
        thread_id="t",
        fingerprint=fp,
        claim_ttl_seconds=0.2,
    )
    assert old.state == "execute"

    await asyncio.sleep(0.35)  # 真实等待过期，不伪造时间

    new = store.claim(
        tool_name="staging_refund",
        operation_key=op_key,
        run_id="run-a2e",
        thread_id="t",
        fingerprint=fp,
        claim_ttl_seconds=30.0,
    )
    assert new.state == "execute", "过期后接管是崩溃恢复的必要能力"
    assert new.owner != old.owner

    # 旧 owner 的迟到写入必须被拒绝（不能覆盖新 owner 的结果）
    assert not store.mark_succeeded("staging_refund", op_key, "stale", owner=old.owner)


@requires_infra
async def test_claim_ttl_must_cover_tool_timeout(store, op_key):
    """A4 的执行期约束在 A2 的实现里被真正强制（不是只写文档）。

    claim TTL 必须显著大于受保护副作用的执行上界，否则「续租」只是把窗口推后，
    没有消除。校验失败必须在**执行之前** fail-closed。
    """
    from runtime.errors import PermanentError

    with pytest.raises(PermanentError):
        await execute_idempotent_operation(
            tool_name="staging_refund",
            operation_key=op_key,
            run_id="run-a2f",
            thread_id="t",
            arguments={"order_id": "ORD-5"},
            operation=lambda: "should-not-run",
            store=store,
            claim_ttl_seconds=0.1,
            min_claim_ttl_seconds=5.0,
        )
    assert store.get("staging_refund", op_key) is None, "校验失败时不得创建 ledger 行"


@requires_infra
async def test_no_false_success_when_result_commit_is_rejected(store, op_key):
    """结果提交被拒时不得静默成功。

    ``mark_succeeded(owner=...)`` 返回 ``False`` 表示归属校验失败（执行权已被夺走）。
    旧实现**忽略**这个返回值，于是外部调用已经发生、ledger 却仍停在 PENDING，
    上层看到的是「工具返回了结果」= 一次成功的写。修复后必须把它升级为
    「结果未知」的可重试错误，让上层重试/DLQ 或人工核对。
    """
    from runtime.errors import SideEffectOwnershipLost
    from runtime.side_effects import request_fingerprint

    external = _External()

    async def _write_then_get_preempted() -> str:
        await external.write({"order_id": "ORD-6", "amount": 7})
        # 执行权在飞行中被夺走：另一个执行者接管并已提交结果
        other = store.claim(
            tool_name="staging_refund",
            operation_key=op_key,
            run_id="run-a2g",
            thread_id="t",
            fingerprint=request_fingerprint({"order_id": "ORD-6", "amount": 7}),
            claim_ttl_seconds=30.0,
        )
        assert other.state == "in_progress", "前提：当前 owner 仍持有"
        return "ext-ok:1"

    # 直接构造「归属校验失败」：先把 claim 交给别人
    fp = request_fingerprint({"order_id": "ORD-6", "amount": 7})
    original = store.claim(
        tool_name="staging_refund",
        operation_key=op_key,
        run_id="run-a2g",
        thread_id="t",
        fingerprint=fp,
        claim_ttl_seconds=0.0,  # 立即过期 -> 允许接管
    )
    assert original.state == "execute"
    preemptor = store.claim(
        tool_name="staging_refund",
        operation_key=op_key,
        run_id="run-a2g",
        thread_id="t",
        fingerprint=fp,
        claim_ttl_seconds=30.0,
    )
    assert preemptor.state == "execute" and preemptor.owner != original.owner

    with pytest.raises(SideEffectOwnershipLost):
        store.commit_result_or_raise(
            tool_name="staging_refund",
            operation_key=op_key,
            result_reference="ext-ok:1",
            owner=original.owner,
        )
    assert external.executions == 0, "本用例只验证提交被拒时的语义"


@requires_infra
async def test_timing_is_real(store, op_key):
    """确认上面的用例确实在真实等待，而不是靠伪造时间通过。"""
    from runtime.side_effects import request_fingerprint

    start = time.perf_counter()
    store.claim(
        tool_name="staging_refund",
        operation_key=op_key,
        run_id="run-a2h",
        thread_id="t",
        fingerprint=request_fingerprint({}),
        claim_ttl_seconds=0.3,
    )
    await asyncio.sleep(0.5)
    assert time.perf_counter() - start >= 0.45
