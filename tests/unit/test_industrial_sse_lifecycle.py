"""SSE lifecycle regression: controlled async tasks, no provider calls."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.routes import chat


def _context(task, queue):
    return chat.SSEStreamContext(task, queue, "fixture", None, False, "status", "progress")


@pytest.mark.asyncio
async def test_sse_close_cancels_and_joins_producer():
    queue = asyncio.Queue()
    released = asyncio.Event()

    async def graph():
        try:
            await queue.put({"type": "chunk", "content": "first"})
            await asyncio.Event().wait()
        finally:
            released.set()

    task = asyncio.create_task(graph())
    stream = chat._sse_stream_generator(_context(task, queue))
    await anext(stream)
    await anext(stream)
    assert "first" in await asyncio.wait_for(anext(stream), 1)
    await stream.aclose()
    assert task.cancelled()
    assert released.is_set()


@pytest.mark.asyncio
async def test_sse_bounded_queue_backpressures_slow_reader():
    monkeypatch_queue = chat.SSE_QUEUE_MAX_EVENTS
    filled = asyncio.Event()

    async def graph(sid, query, *, stream_callback, user_id):
        for index in range(monkeypatch_queue + 1):
            await stream_callback({"type": "chunk", "content": str(index)})
            if index == monkeypatch_queue - 1:
                filled.set()
        return {"response": "finished"}

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(run_graph=graph)))
    task, queue = chat._build_sse_stream_context(request, "fixture", "q", None, False)
    try:
        await asyncio.wait_for(filled.wait(), 1)
        assert queue.qsize() == queue.maxsize == chat.SSE_QUEUE_MAX_EVENTS
        assert not task.done()
        events = [event async for event in chat._sse_stream_generator(_context(task, queue))]
        assert len(events) == chat.SSE_QUEUE_MAX_EVENTS + 4
        assert '"type": "done"' in events[-1]
    finally:
        await chat._stop_stream_graph(task)


@pytest.mark.asyncio
async def test_heartbeat_does_not_steal_next_event(monkeypatch):
    monkeypatch.setattr(chat, "SSE_CHUNK_TIMEOUT", 0.01)
    queue = asyncio.Queue()
    proceed = asyncio.Event()

    async def graph():
        await proceed.wait()
        await queue.put({"type": "chunk", "content": "after heartbeat"})
        return {"response": "finished"}

    task = asyncio.create_task(graph())
    stream = chat._sse_stream_generator(_context(task, queue))
    try:
        await anext(stream)
        await anext(stream)
        assert await asyncio.wait_for(anext(stream), 1) == ": heartbeat\n\n"
        proceed.set()
        await task
        assert "after heartbeat" in await anext(stream)
        assert '"type": "done"' in await anext(stream)
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_managed_response_cleans_graph_before_first_frame(monkeypatch):
    async def send_failure(self, scope, receive, send):
        raise OSError("fixture disconnected")

    monkeypatch.setattr(chat.StreamingResponse, "__call__", send_failure)
    queue = asyncio.Queue()
    task = asyncio.create_task(asyncio.sleep(3600))
    response = chat.ManagedSSE(_context(task, queue), media_type="text/event-stream")
    with pytest.raises(OSError):
        await response({}, AsyncMock(), AsyncMock())
    assert task.cancelled()


@pytest.mark.asyncio
async def test_industrial_graph_typeerror_never_invokes_compatibility_retry(monkeypatch):
    from api import app
    from core.session import ownership

    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "industrial")
    monkeypatch.setattr(ownership, "require_session_owner", AsyncMock())
    monkeypatch.setattr(
        app,
        "_session_manager",
        SimpleNamespace(create_session=AsyncMock(), set_user_id=lambda *args: None),
    )
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(next=(), tasks=(), values={})),
        ainvoke=AsyncMock(side_effect=TypeError("fixture node failed")),
    )
    monkeypatch.setattr(app, "_graph_app", graph)
    monkeypatch.setattr("core.config.AGENT_RUN_THREAD_LOCK_ENABLED", False)
    with pytest.raises(TypeError, match="fixture node failed"):
        await app._run_graph_inner("fixture", "q", user_id="user")
    graph.ainvoke.assert_awaited_once()
