"""
Unit tests for _process_with_tools streaming events.
v6.0: 验证 thinking / tool_call / tool_result 事件的正确发射。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.base_agent import BaseAgent


class DummyAgent(BaseAgent):
    """Concrete subclass for testing BaseAgent methods."""

    async def process(self, state):
        return state


@pytest.mark.unit
async def test_process_with_tools_emits_thinking_and_tool_call():
    """_process_with_tools 在工具调用时 emit thinking + tool_call + tool_result 事件"""
    agent = DummyAgent(name="test_agent", role="test", expertise=["test"])
    agent.logger = MagicMock()

    events = []

    async def mock_cb(event):
        events.append(event)

    # Mock LLM — first call returns tool_calls, second returns final answer
    mock_first_response = MagicMock()
    mock_first_response.tool_calls = [
        {"id": "call_1", "name": "query_product", "arguments": '{"keyword": "烟酰胺"}'}
    ]
    mock_first_response.content = ""

    mock_second_response = MagicMock()
    mock_second_response.tool_calls = None
    mock_second_response.content = "烟酰胺是维生素B3的一种形式。"

    mock_llm = AsyncMock()
    mock_llm.async_invoke = AsyncMock(side_effect=[mock_first_response, mock_second_response])

    # async_invoke_stream must be a real async generator for async for to work
    async def mock_stream(messages, timeout=None):
        for chunk in ["烟", "酰", "胺", "是", "维", "生", "素", "B3"]:
            yield chunk

    mock_llm.async_invoke_stream = mock_stream
    agent.llm = mock_llm

    # Mock tool_registry
    mock_tool = MagicMock()
    mock_tool.type = "function"
    mock_tool.function = MagicMock()

    mock_registry = AsyncMock()
    mock_registry.get_openai_tools.return_value = [mock_tool]
    mock_registry.execute = AsyncMock(return_value="成分说明：烟酰胺（维生素B3）…")
    agent.tool_registry = mock_registry

    # Mock base methods
    agent._resolve_prompt_for_variant = MagicMock(return_value=("prompt", "control", None))
    agent._prepare_llm_messages = AsyncMock(return_value=("sid", [], False))
    agent._add_message_to_session = AsyncMock()
    agent._publish_event = AsyncMock()
    agent._try_rule_fallback = AsyncMock(return_value=None)

    state = {
        "customer_query": "烟酰胺是什么？",
        "stream_callback": mock_cb,
        "session_id": "test-session",
    }

    result = await agent._process_with_tools(state, "你是一个产品专家")

    event_types = [e["type"] for e in events]
    assert "thinking" in event_types, f"缺少 thinking 事件, got {event_types}"
    assert "tool_call" in event_types, f"缺少 tool_call 事件, got {event_types}"
    assert "tool_result" in event_types, f"缺少 tool_result 事件, got {event_types}"

    tool_call_events = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_call_events) >= 1
    assert tool_call_events[0]["name"] == "query_product"

    tool_result_events = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_result_events) >= 1
    assert "成分说明" in tool_result_events[0]["summary"]


@pytest.mark.unit
async def test_process_with_tools_no_tool_call_no_extra_events():
    """_process_with_tools 没有 tool_call 时不应 emit tool_call/tool_result"""
    agent = DummyAgent(name="test_agent", role="test", expertise=["test"])
    agent.logger = MagicMock()

    events = []

    async def mock_cb(event):
        events.append(event)

    mock_response = MagicMock()
    mock_response.tool_calls = None
    mock_response.content = "直接回答"

    mock_llm = AsyncMock()
    mock_llm.async_invoke = AsyncMock(return_value=mock_response)

    async def mock_stream(messages, timeout=None):
        for chunk in ["直", "接", "回", "答"]:
            yield chunk

    mock_llm.async_invoke_stream = mock_stream
    agent.llm = mock_llm

    mock_tool = MagicMock()
    mock_tool.type = "function"
    mock_tool.function = MagicMock()

    mock_registry = AsyncMock()
    mock_registry.get_openai_tools.return_value = [mock_tool]
    agent.tool_registry = mock_registry

    agent._resolve_prompt_for_variant = MagicMock(return_value=("prompt", "control", None))
    agent._prepare_llm_messages = AsyncMock(return_value=("sid", [], False))
    agent._add_message_to_session = AsyncMock()
    agent._publish_event = AsyncMock()
    agent._try_rule_fallback = AsyncMock(return_value=None)

    state = {
        "customer_query": "你好",
        "stream_callback": mock_cb,
        "session_id": "test-session",
    }

    result = await agent._process_with_tools(state, "你是一个客服")

    tool_events = [e for e in events if e["type"] in ("tool_call", "tool_result")]
    assert len(tool_events) == 0
    assert result["response"] == "直接回答"