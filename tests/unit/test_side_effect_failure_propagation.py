"""Durable write failures must escape the real compiled StateGraph.

A failed tool is not a successful AgentRun. Read-only failures, on the other
hand, may be represented as explainable results without crashing the request.
Tests use a scripted LLM and no real ERP writes.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tools.tool_registry import SideEffectExecutionError, ToolExecutionReceipt, ToolReportedFailure

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("as_exception", [True, False])
async def test_durable_write_failure_escapes_real_graph(monkeypatch, as_exception):
    from core.container import ServiceContainer
    from runtime.context import reset_run_context, set_run_context
    from tests.unit.test_hitl_real_graph_gate import _drain_graph, _prepare

    monkeypatch.setattr("core.config.HITL_ENABLED", False)

    container = ServiceContainer()
    await container.initialize()
    try:
        _prepare(container, "staging_refund", {"order_id": "FAIL-EVIDENCE-1", "amount": 99})

        async def fail_write(*args, **kwargs):
            if as_exception:
                raise ToolReportedFailure("staging refused write")
            return ToolExecutionReceipt("错误：写操作未执行", ok=False)

        monkeypatch.setattr(container.tool_registry, "execute_raw", fail_write)

        thread = f"write-failure-{uuid4().hex}"
        tokens = set_run_context(f"run-{uuid4().hex}", thread)
        try:
            with pytest.raises(SideEffectExecutionError):
                await _drain_graph(container, thread_id=thread)
        finally:
            reset_run_context(tokens)
    finally:
        await container.close()


async def test_realtime_side_effect_refusal_is_not_a_graph_crash(monkeypatch):
    """Without durable run context, writes remain refused but can explain why."""
    from core.container import ServiceContainer
    from tests.unit.test_hitl_real_graph_gate import _drain_graph, _prepare

    monkeypatch.setattr("core.config.HITL_ENABLED", False)
    container = ServiceContainer()
    await container.initialize()
    try:
        _prepare(container, "staging_refund", {"order_id": "NO-RUN-1", "amount": 99})
        observed = await _drain_graph(container, thread_id=f"no-run-{uuid4().hex}")
        assert not any(item.get("ok") for item in observed.get("tool_executions") or [])
        assert observed["outcome"].outcome_verified is False
    finally:
        await container.close()
