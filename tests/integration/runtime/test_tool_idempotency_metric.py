"""工具副作用幂等（application-level 去重）的**进程内**断言（Gate 11 指标部分）。

worker 进程的 Prometheus registry 无法从外部读取，因此指标递增在这里就地断言；
跨进程的"重复投递下去重生效"证据见 ``test_tool_idempotency.py``。

运行::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    pytest tests/integration/runtime/test_tool_idempotency_metric.py -q
"""

from __future__ import annotations

import os

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not DB_URL, reason="TEST_DISTRIBUTED_DB_URL 未设置；需要真实 PostgreSQL"),
]


def _counter(name: str) -> float:
    from prometheus_client import REGISTRY

    total = 0.0
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name in (name, f"{name}_total"):
                total += sample.value
    return total


def test_gate11_idempotency_hit_metric_increments(side_effect_store, unique):
    """第二次同 key 调用命中 ledger 时，``agent_tool_idempotency_hit_total`` 必须 +1。"""
    from runtime import metrics as run_metrics
    from runtime.side_effects import execute_idempotent_operation

    tool_name = "increment_counter"
    op_key = f"{unique('run')}:call-1"
    args = {"amount": 100}

    calls = {"n": 0}

    def side_effect() -> dict:
        calls["n"] += 1
        return {"ok": True, "seq": calls["n"]}

    before = _counter("agent_tool_idempotency_hit_total")

    import asyncio

    first = asyncio.run(
        execute_idempotent_operation(
            tool_name=tool_name,
            operation_key=op_key,
            run_id=unique("run"),
            thread_id=unique("T"),
            arguments=args,
            operation=side_effect,
            store=side_effect_store,
        )
    )
    assert calls["n"] == 1

    second = asyncio.run(
        execute_idempotent_operation(
            tool_name=tool_name,
            operation_key=op_key,
            run_id=unique("run"),
            thread_id=unique("T"),
            arguments=args,
            operation=side_effect,
            store=side_effect_store,
        )
    )

    assert calls["n"] == 1, "命中幂等 ledger 时不得再次执行副作用"
    assert second == first, "必须返回第一次执行的结果"
    after = _counter("agent_tool_idempotency_hit_total")
    assert after >= before + 1, f"幂等命中指标未递增: {before} -> {after}"

    # 直接调用转发器同样生效（保证指标接线不依赖调用方）
    mid = _counter("agent_tool_idempotency_hit_total")
    run_metrics.record_tool_idempotency_hit()
    assert _counter("agent_tool_idempotency_hit_total") == mid + 1


def test_gate11_conflicting_arguments_are_permanent_error(side_effect_store, unique):
    """同 key 不同参数 = 业务冲突，必须 permanent（不重试、不重复执行）。"""
    from runtime.errors import PermanentError
    from runtime.side_effects import execute_idempotent_operation

    op_key = f"{unique('run')}:call-1"
    calls = {"n": 0}

    def side_effect() -> dict:
        calls["n"] += 1
        return {"ok": True}

    import asyncio

    asyncio.run(
        execute_idempotent_operation(
            tool_name="increment_counter",
            operation_key=op_key,
            run_id=unique("run"),
            thread_id=unique("T"),
            arguments={"amount": 100},
            operation=side_effect,
            store=side_effect_store,
        )
    )
    with pytest.raises(PermanentError):
        asyncio.run(
            execute_idempotent_operation(
                tool_name="increment_counter",
                operation_key=op_key,
                run_id=unique("run"),
                thread_id=unique("T"),
                arguments={"amount": 999},
                operation=side_effect,
                store=side_effect_store,
            )
        )
    assert calls["n"] == 1, "参数冲突时不得执行第二次副作用"


def test_gate11_pending_claim_lease_blocks_concurrent_execution(side_effect_store, unique):
    """认领租约未过期时，第二个执行者不得重复触发副作用（抛 transient 退避）。"""
    from sqlalchemy import select

    from db.models import ToolSideEffect
    from runtime.errors import TransientError
    from runtime.side_effects import STATUS_PENDING, _utcnow, request_fingerprint

    tool_name = "increment_counter"
    op_key = f"{unique('run')}:call-1"
    run_id = unique("run")
    fp = request_fingerprint({"amount": 1})

    first = side_effect_store.claim(
        tool_name=tool_name,
        operation_key=op_key,
        run_id=run_id,
        thread_id=unique("T"),
        fingerprint=fp,
        claim_ttl_seconds=60.0,
    )
    assert first.state == "execute"

    # 模拟"另一个 worker 立刻重投"：租约仍在，不允许再执行一次
    second = side_effect_store.claim(
        tool_name=tool_name,
        operation_key=op_key,
        run_id=run_id,
        thread_id=unique("T"),
        fingerprint=fp,
        claim_ttl_seconds=60.0,
    )
    assert second.state == "in_progress"

    # 租约过期（认领后崩溃）后允许接管重放
    from datetime import timedelta

    session = side_effect_store._session()
    try:
        row = session.execute(
            select(ToolSideEffect).where(
                ToolSideEffect.tool_name == tool_name,
                ToolSideEffect.operation_key == op_key,
            )
        ).scalar_one()
        assert row.status == STATUS_PENDING
        # 过期判定的权威字段是 ``claim_expires_at``（migration 005 引入的认领租约）；
        # ``updated_at`` 只是老数据回退路径。只回拨 ``updated_at`` 不会让租约过期，
        # 因此两者都要回拨，才能真正模拟"认领后崩溃、租约到期"。
        row.claim_expires_at = _utcnow() - timedelta(seconds=600)
        row.updated_at = _utcnow() - timedelta(seconds=600)
        session.commit()
    finally:
        session.close()

    third = side_effect_store.claim(
        tool_name=tool_name,
        operation_key=op_key,
        run_id=run_id,
        thread_id=unique("T"),
        fingerprint=fp,
        claim_ttl_seconds=60.0,
    )
    assert third.state == "execute", "租约过期后必须允许接管，否则崩溃会永久卡住"

    # 并且 execute_idempotent_operation 对 in_progress 抛 transient（退避重投）
    from runtime.side_effects import execute_idempotent_operation

    with pytest.raises(TransientError):
        import asyncio

        asyncio.run(
            execute_idempotent_operation(
                tool_name=tool_name,
                operation_key=op_key,
                run_id=run_id,
                thread_id=unique("T"),
                arguments={"amount": 1},
                operation=lambda: {"ok": True},
                store=side_effect_store,
                claim_ttl_seconds=600.0,
            )
        )
