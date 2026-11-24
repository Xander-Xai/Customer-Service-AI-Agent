"""Consultation must preserve execution evidence and disclose partial failures."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from collaboration.modes import ConsultationMode
from core.outcome import classify_outcome


def _agent(result=None, *, failure=None):
    agent = AsyncMock()
    agent.process_with_retry = AsyncMock(return_value=result, side_effect=failure)
    return agent


@pytest.mark.asyncio
async def test_auxiliary_approval_and_receipts_survive_primary_aggregation():
    pending = {"tool": "refund", "arguments": {"order_id": "synthetic"}}
    receipt = {"tool": "lookup_order", "ok": True}
    primary_receipt = {"tool": "lookup_product", "ok": True}
    primary = _agent({"response": "处理建议", "tool_executions": [primary_receipt]})
    auxiliary = _agent(
        {"response": "补充", "pending_actions": [pending], "tool_executions": [receipt]}
    )
    result = await ConsultationMode().execute(
        {"product": primary, "billing": auxiliary},
        {"customer_query": "合成订单问题"},
        {"primary_agent": "product", "consult_agents": ["billing"]},
    )
    assert result["pending_actions"] == [pending]
    assert result["tool_executions"] == [receipt, primary_receipt]
    assert classify_outcome(result).requires_human_action
    assert "补充" in primary.process_with_retry.call_args.args[0]["customer_query"]


@pytest.mark.asyncio
async def test_partial_auxiliary_failure_is_degraded_not_silent():
    primary = _agent({"response": "处理建议"})
    result = await ConsultationMode().execute(
        {"product": primary, "billing": _agent(failure=RuntimeError("private backend text"))},
        {"customer_query": "合成订单问题"},
        {"primary_agent": "product", "consult_agents": ["billing"]},
    )
    assert result["degraded_reason"] == "consultation_agent_failed"
    assert classify_outcome(result).degraded
    assert "private backend text" not in str(result)
    assert result["tool_executions"] == []


@pytest.mark.asyncio
async def test_missing_auxiliary_is_degraded():
    result = await ConsultationMode().execute(
        {"product": _agent({"response": "处理建议"})},
        {"customer_query": "合成订单问题"},
        {"primary_agent": "product", "consult_agents": ["missing"]},
    )
    assert result["degraded_reason"] == "consultation_agent_missing"


@pytest.mark.asyncio
async def test_timeout_preserves_completed_auxiliary_receipt():
    receipt = {"tool": "lookup_order", "ok": True}
    completed = asyncio.Event()

    async def fast(state):
        completed.set()
        return {"response": "查询结果", "tool_executions": [receipt]}

    async def slow(state):
        await completed.wait()
        await asyncio.Event().wait()

    a, b = _agent(), _agent()
    a.process_with_retry.side_effect = fast
    b.process_with_retry.side_effect = slow
    result = await ConsultationMode(consult_timeout=0.1).execute(
        {"product": _agent({"response": "处理建议"}), "fast": a, "slow": b},
        {"customer_query": "合成订单问题"},
        {"primary_agent": "product", "consult_agents": ["fast", "slow"]},
    )
    assert result["degraded_reason"] == "consultation_timeout"
    assert result["tool_executions"] == [receipt]
    assert classify_outcome(result).degraded
