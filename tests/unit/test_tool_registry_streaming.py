import pytest
from unittest.mock import AsyncMock


@pytest.mark.unit
async def test_tool_execute_forwards_stream_callback():
    """ToolRegistry.execute 将 stream_callback 转发给 tool handler"""
    from tools.tool_registry import ToolRegistry

    registry = ToolRegistry()
    handler_events = []

    async def test_handler(args, stream_callback=None):
        if stream_callback:
            await stream_callback({"type": "rag_status", "status": "retrieving"})
            handler_events.append(("called", args))
            await stream_callback({"type": "rag_status", "status": "done", "count": 3})
        return "result"

    registry.register("test_tool", "Test", {"type": "object", "properties": {}}, test_handler)

    cb_events = []
    async def mock_cb(e):
        cb_events.append(e)

    result = await registry.execute("test_tool", {"query": "test"}, stream_callback=mock_cb)
    assert result == "result"
    assert len(cb_events) == 2
    assert cb_events[0] == {"type": "rag_status", "status": "retrieving"}
    assert cb_events[1] == {"type": "rag_status", "status": "done", "count": 3}


@pytest.mark.unit
async def test_tool_execute_no_stream_callback():
    """无 stream_callback 时向后兼容"""
    from tools.tool_registry import ToolRegistry

    registry = ToolRegistry()

    async def handler(args):
        return "normal result"

    registry.register("t", "Test", {"type": "object", "properties": {}}, handler)
    result = await registry.execute("t", {})
    assert result == "normal result"
