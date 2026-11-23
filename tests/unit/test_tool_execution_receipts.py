"""Execution-evidence regression tests: transport success is not business evidence.

These tests use a real ToolRegistry, and one test runs the compiled production
StateGraph with a deterministic scripted LLM (no network or real ERP writes).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tools.tool_registry import (
    ToolExecutionReceipt,
    ToolRegistry,
    ToolReportedFailure,
)

pytestmark = pytest.mark.unit


def _register(name, handler):
    registry = ToolRegistry()
    registry.register(
        name=name,
        description="execution evidence test",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    return registry


async def test_receipt_success_preserves_raw_and_structured_payloads():
    async def handler(_args):
        return {"items": [1, 2, 3]}

    registry = _register("lookup", handler)
    assert await registry.execute_raw("lookup", {}) == {"items": [1, 2, 3]}
    receipt = await registry.execute_raw("lookup", {}, return_receipt=True)
    assert isinstance(receipt, ToolExecutionReceipt)
    assert receipt.ok is True
    assert receipt.value == {"items": [1, 2, 3]}


@pytest.mark.parametrize(
    "payload",
    [
        "错误：缺少 order_id",
        "工具 'lookup' 执行失败，请稍后重试",
        "工具暂时不可用，请稍后重试",
        {"success": False, "error": "denied"},
        {"status": "rejected"},
        None,
    ],
)
async def test_explicit_failure_is_never_successful_execution(payload):
    async def handler(_args):
        return payload

    registry = _register("lookup", handler)
    receipt = await registry.execute_raw("lookup", {}, return_receipt=True)
    assert receipt.ok is False


async def test_unknown_tool_receipt_is_not_success():
    registry = ToolRegistry()
    receipt = await registry.execute_raw("not_registered", {}, return_receipt=True)
    assert receipt.ok is False
    assert "不存在" in receipt.value


async def test_handler_exception_receipt_is_not_success():
    async def handler(_args):
        raise ValueError("downstream unavailable")

    registry = _register("lookup", handler)
    receipt = await registry.execute_raw("lookup", {}, return_receipt=True)
    assert receipt.ok is False


async def test_failed_side_effect_cannot_be_recorded_as_success():
    async def handler(_args):
        return "错误：退款参数无效"

    registry = ToolRegistry()
    registry.register(
        name="refund",
        description="staging only",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        side_effect=True,
    )
    # This is the same _call_handler path wrapped inside the side-effect
    # ledger. Raising ensures the ledger cannot commit a failed string.
    with pytest.raises(ToolReportedFailure):
        await registry._call_handler(registry._tools["refund"], {}, None, raise_on_timeout=True)


async def test_real_graph_forwards_successful_read_only_tool_evidence():
    from core.container import ServiceContainer
    from tests.unit.test_hitl_real_graph_gate import _drain_graph, _prepare

    container = ServiceContainer()
    await container.initialize()
    try:
        _prepare(container, "staging_readonly_lookup", {"order_id": "EVIDENCE-1"})
        observed = await _drain_graph(container, thread_id=f"evidence-read-{uuid4().hex}")
        executions = observed.get("tool_executions") or []
        assert any(
            item.get("tool") == "staging_readonly_lookup" and item.get("ok") for item in executions
        ), "An actual successful tool call was lost at the collaboration -> graph boundary"
        outcome = observed["outcome"]
        assert outcome.outcome_verified is True
    finally:
        await container.close()


async def test_real_graph_does_not_verify_failed_tool():
    from core.container import ServiceContainer
    from tests.unit.test_hitl_real_graph_gate import _drain_graph, _prepare

    container = ServiceContainer()
    await container.initialize()
    try:
        _prepare(container, "broken_lookup", {})

        async def handler(_args):
            return "错误：下游查询被拒绝"

        container.tool_registry.register(
            name="broken_lookup",
            description="failing read-only tool",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        )
        observed = await _drain_graph(container, thread_id=f"evidence-fail-{uuid4().hex}")
        executions = observed.get("tool_executions") or []
        assert any(
            item.get("tool") == "broken_lookup" and item.get("ok") is False for item in executions
        )
        assert observed["outcome"].outcome_verified is False
    finally:
        await container.close()


async def test_new_turn_does_not_inherit_prior_tool_execution_evidence():
    """Checkpointed thread state must not turn a second text-only answer into evidence."""
    from core.container import ServiceContainer
    from tests.unit.test_hitl_real_graph_gate import _drain_graph, _prepare

    container = ServiceContainer()
    await container.initialize()
    try:
        _prepare(container, "staging_readonly_lookup", {"order_id": "TURN-1"})
        thread_id = f"evidence-two-turns-{uuid4().hex}"
        first = await _drain_graph(container, thread_id=thread_id)
        assert any(item.get("ok") for item in first.get("tool_executions") or [])

        # The scripted LLM emits tools only on its first invocation. The next
        # request reuses the same checkpointed thread, but executes no tool.
        second = await _drain_graph(container, thread_id=thread_id)
        assert second.get("tool_executions") == []
        assert second.get("pending_actions") == []
        assert second.get("approval_results") == []
        assert second["outcome"].outcome_verified is False
    finally:
        await container.close()
