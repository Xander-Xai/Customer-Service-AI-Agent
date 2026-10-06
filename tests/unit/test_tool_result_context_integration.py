import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base_agent import BaseAgent
from core.tool_result_optimizer import ToolResultOptimizer
from core.tool_result_store import InMemoryToolResultStore


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
        response.tool_calls = [
            {
                "id": f"call-{number}",
                "name": "query_order",
                "arguments": {"order_id": f"O{number}"},
            }
        ]
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
    registry.execute = AsyncMock(
        return_value=[
            {"order_id": "O1", "status": "shipped", "debug": "x"},
        ]
    )
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


@pytest.mark.asyncio
async def test_agent_offloads_large_result_and_recovers_through_application_hook():
    agent = DummyAgent(name="offload-agent", role="test", expertise=["test"])
    store = InMemoryToolResultStore()
    agent.set_tool_result_optimizer(
        ToolResultOptimizer(enabled=True, store=store, offload_enabled=True, offload_min_tokens=1)
    )
    response = MagicMock(
        content="", tool_calls=[{"id": "call-offload", "name": "query_order", "arguments": {}}]
    )
    final = MagicMock(content="已完成", tool_calls=[])
    llm = MagicMock()
    llm.async_invoke = AsyncMock(side_effect=[response, final])
    agent.set_llm(llm)
    registry = MagicMock()
    registry.get_openai_tools.return_value = []
    raw = [{"order_id": "O1", "description": "large" * 100}]
    registry.execute_raw = AsyncMock(return_value=raw)
    agent.set_tool_registry(registry)

    result = await agent._process_with_tools(
        {"session_id": "s1", "user_id": "u1", "customer_query": "查订单"},
        "你是客服",
        max_tool_rounds=2,
    )
    assert result["response"] == "已完成"
    tool_message = next(
        m for m in llm.async_invoke.call_args_list[1].args[0] if getattr(m, "type", None) == "tool"
    )
    preview = json.loads(tool_message.content)
    assert preview["status"] == "result_offloaded"
    assert (
        await agent.recover_tool_result(
            preview["reference_id"], {"session_id": "s1", "user_id": "u1"}
        )
        == raw
    )
    assert (
        await agent.recover_tool_result(
            preview["reference_id"], {"session_id": "s2", "user_id": "u2"}
        )
        is None
    )
