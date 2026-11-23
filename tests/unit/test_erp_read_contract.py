"""Direct ERP read path: bounded deadline and truthful degraded outcome.

This path is read-only and uses the existing authorized ERP adapter. It does
not grant writes or fabricate ToolRegistry execution receipts.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from agents.base_agent import BaseAgent
from core.outcome import classify_outcome

pytestmark = pytest.mark.unit


class _ReadAgent(BaseAgent):
    async def process(self, state):
        return state


@pytest.fixture
def agent():
    inst = _ReadAgent(name="erp-contract-test", role="test", expertise=["read"])
    inst.logger = MagicMock()
    return inst


async def test_success_carries_explicit_erp_read_status(agent):
    async def read():
        return {"status": "shipped"}

    state = {"response": "订单已发出"}
    result = await agent._safe_erp_query(read, state=state)
    assert result == {"status": "shipped"}
    assert state["erp_read_status"] == "ok"
    assert "degraded_reason" not in state


async def test_exception_is_degraded_and_exception_body_not_logged(agent):
    async def read():
        raise PermissionError("secret-customer-address")

    state = {"response": "已查询到订单"}
    result = await agent._safe_erp_query(read, state=state)
    assert "ERP 暂时不可用" in result
    assert state["erp_read_status"] == "error"
    assert state["degraded_reason"] == "erp_read_error"
    outcome = classify_outcome(state)
    assert outcome.degraded
    assert not outcome.outcome_verified
    logged = repr(agent.logger.warning.call_args)
    assert "secret-customer-address" not in logged


async def test_erp_timeout_cannot_be_mistaken_for_a_success(agent, monkeypatch):
    monkeypatch.setattr("core.config.TOOL_EXECUTION_TIMEOUT_SECONDS", 0.001)

    async def slow():
        await asyncio.sleep(0.05)
        return {"status": "shipped"}

    state = {"response": "已查询到订单"}
    result = await agent._safe_erp_query(slow, state=state)
    assert "ERP 暂时不可用" in result
    assert state["erp_read_status"] == "timeout"
    assert state["degraded_reason"] == "erp_read_timeout"
    assert classify_outcome(state).degraded


async def test_existing_read_fallback_contract_without_state(agent):
    async def read():
        raise ConnectionError("secret")

    assert await agent._safe_erp_query(read, fallback="offline") == "offline"


async def test_previous_degradation_reason_not_overwritten(agent):
    async def read():
        raise ConnectionError("test")

    state = {"degraded_reason": "embedding_unavailable"}
    await agent._safe_erp_query(read, state=state)
    assert state["degraded_reason"] == "embedding_unavailable"
    assert state["erp_read_status"] == "error"
