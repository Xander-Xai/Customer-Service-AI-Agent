from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base_agent import BaseAgent
from core.tool_result_optimizer import ToolResultOptimizer


class DummyAgent(BaseAgent):
    async def process(self, state):
        return state


@pytest.mark.asyncio
async def test_enabled_optimizer_compacts_old_results_and_preserves_tool_call_ids():
    agent = DummyAgent(name="context-agent", role="test", expertise=["test"])
    agent.set_tool_result_optimizer(ToolResultOptimizer(enabled=True))
    agent._resolve_prompt_for_variant = MagicMock(return_value=("prompt", "control", None))
    agent._prepare_llm_messages = AsyncMock(return_value=("sid", [], False))
    agent._add_message_to_session = AsyncMock()
    agent._publish_event = AsyncMock()

    responses = []
    for number in range(3):
        response = MagicMock()
        response.content = ""
        response.tool_calls = [{
            "id": f"call-{number}",
            "name": "query_order",
            "arguments": {"order_id": f"O{number}"},
        }]
        responses.append(response)
    final = MagicMock(content="最终回答", tool_calls=[])
    captured = []

    async def invoke(messages, tools=None):
        captured.append(list(messages))
        return responses.pop(0) if responses else final

    llm = MagicMock()
    llm.async_invoke = AsyncMock(side_effect=invoke)
    agent.set_llm(llm)
    registry = MagicMock()
    registry.get_openai_tools.return_value = []
    registry.execute = AsyncMock(return_value=[
        {"order_id": "O1", "status": "shipped", "debug": "x"},
    ])
    agent.set_tool_registry(registry)

    result = await agent._process_with_tools(
        {"session_id": "context-test", "customer_query": "查订单"},
        "你是客服",
        max_tool_rounds=4,
    )

    assert result["response"] == "最终回答"
    assert len(captured) == 4
    old_tool_messages = [m for m in captured[-1] if getattr(m, "type", None) == "tool"]
    assert [m.tool_call_id for m in old_tool_messages] == ["call-0", "call-1", "call-2"]
    assert "older tool result compacted" in old_tool_messages[0].content
    assert "older tool result compacted" not in old_tool_messages[-1].content
