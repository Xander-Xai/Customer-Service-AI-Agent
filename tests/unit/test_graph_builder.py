"""
测试 core/graph_builder.py — _emit_status 辅助函数（v6.0: 全链路 SSE 流式）
"""

import pytest

from core.graph_builder import _emit_status


@pytest.mark.unit
async def test_emit_status_sends_events():
    """验证 _emit_status 辅助函数正确发出 status SSE 事件"""
    events = []

    async def mock_cb(event):
        events.append(event)

    state = {"stream_callback": mock_cb}

    await _emit_status(state, "cache", "🔍 检查缓存中...")
    assert len(events) == 1
    assert events[0] == {"type": "status", "phase": "cache", "content": "🔍 检查缓存中..."}


@pytest.mark.unit
async def test_emit_status_no_callback():
    """stream_callback 不存在时不报错"""
    state = {}  # no stream_callback
    await _emit_status(state, "cache", "test")  # should not raise


@pytest.mark.unit
async def test_emit_status_callback_error_logged():
    """callback 异常时不应传播，只 log 并继续"""

    async def failing_cb(_event):
        raise Exception("boom")

    state = {"stream_callback": failing_cb}
    await _emit_status(state, "cache", "test")  # should not raise
