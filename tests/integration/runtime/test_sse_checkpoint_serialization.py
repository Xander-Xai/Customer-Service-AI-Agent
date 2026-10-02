"""SSE + checkpointer 的序列化回归（Gate 0 / P0 bug 修复）。

缺陷（修复前真实存在）：``stream_callback`` 是 async 可调用对象，却被声明为
``core/state.py`` 的 LangGraph channel。LangGraph 会把**每个 channel** 交给
checkpointer 序列化，于是 ``POST /api/chat/stream`` 只要开启 checkpoint 就必然失败::

    TypeError: Type is not msgpack serializable: function

MemorySaver 与官方 ``AsyncPostgresSaver`` 都会失败（共用 ``JsonPlusSerializer``），
而生产默认 ``LANGGRAPH_CHECKPOINT_BACKEND=postgres`` —— 也就是说 SSE 在生产是坏的。

修复：回调移出 channel，改用 contextvar（``core/streaming_context.py``）。

本文件锁定两件事：
  1. 回调**不再**出现在 checkpointed state 中（真实 PG checkpointer 可写入）；
  2. 节点仍能拿到回调（SSE 功能没被"修没了"）。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from core.streaming_context import (
    get_stream_callback,
    reset_stream_callback,
    set_stream_callback,
)

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not DB_URL, reason="TEST_DISTRIBUTED_DB_URL 未设置；需要真实 PostgreSQL"),
]


def test_unit_state_must_not_receive_callable():
    """纯单元层：``_run_graph`` 不再把回调写进 state。

    这是本次修复的核心断言——state 是 checkpoint 的来源，可调用对象一旦进入，
    任何 checkpointer 写盘都会失败。
    """
    import inspect

    from api.app import _run_graph

    src = inspect.getsource(_run_graph)
    assert 'state["stream_callback"]' not in src, (
        "_run_graph 不得再把 async 可调用对象放进 LangGraph state "
        "(checkpointer 无法序列化 function)"
    )
    assert "set_stream_callback" in src, "应改用 contextvar 传递流式回调"


def test_unit_getter_prefers_state_for_backward_compat():
    """get_stream_callback 仍兼容"回调放在 state"的老调用方。"""
    calls = []

    async def cb(event):  # pragma: no cover - 仅作为可调用对象
        calls.append(event)

    assert get_stream_callback({"stream_callback": cb}) is cb
    # state 里没有时回退到 contextvar
    assert get_stream_callback({"q": 1}) is None
    token = set_stream_callback(cb)
    try:
        assert get_stream_callback() is cb
        assert get_stream_callback({"q": 1}) is cb
    finally:
        reset_stream_callback(token)
    assert get_stream_callback() is None


@pytest.mark.timeout(180)
def test_postgres_checkpointer_accepts_streaming_state_and_still_streams():
    """真实 PG checkpointer：写盘成功 + 回调仍到达节点。"""
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    from core.checkpointer import build_postgres_checkpointer

    class S(TypedDict, total=False):
        q: str
        out: str

    seen: list[dict] = []

    async def node(state: S) -> dict:
        cb = get_stream_callback(state)
        if cb is not None:
            await cb({"type": "chunk", "content": "from-node"})
        return {"out": state.get("out", "") + "!"}

    async def scenario():
        rt = await build_postgres_checkpointer(DB_URL, min_size=1, max_size=2)
        graph = (
            StateGraph(S)
            .add_node("n", node)
            .add_edge(START, "n")
            .add_edge("n", END)
            .compile(checkpointer=rt.checkpointer)
        )
        thread_id = f"stream-{uuid.uuid4().hex[:10]}"

        events: list[dict] = []

        async def cb(event):
            events.append(event)

        token = set_stream_callback(cb)
        try:
            result = await graph.ainvoke(
                {"q": "a"}, config={"configurable": {"thread_id": thread_id}}
            )
        finally:
            reset_stream_callback(token)
        return result, events, thread_id

    result, events, thread_id = asyncio.run(scenario())

    # 写盘成功（修复前这里抛 TypeError: function is not msgpack serializable）
    assert result["out"] == "!", result
    # 回调确实到达了节点（SSE 功能保留）
    assert events == [{"type": "chunk", "content": "from-node"}], events

    # checkpoint 里不含函数（能反序列化即证明）
    async def verify(graph):
        cfg = {"configurable": {"thread_id": thread_id}}
        state = await graph.aget_state(cfg)
        assert state is not None, "checkpoint 应存在"
        assert "stream_callback" not in (state.values or {}), (
            "checkpointed state 不应含流式回调"
        )

    graph_for_verify = asyncio.run(_rebuild_graph(DB_URL, node))
    asyncio.run(verify(graph_for_verify))
    seen.append({"thread_id": thread_id})


async def _rebuild_graph(db_url: str, node):
    """重建一个指向同一 checkpointer 的编译图（只读校验用）。"""
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    from core.checkpointer import build_postgres_checkpointer

    class S(TypedDict, total=False):
        q: str
        out: str

    rt = await build_postgres_checkpointer(db_url, min_size=1, max_size=2)
    return (
        StateGraph(S)
        .add_node("n", node)
        .add_edge(START, "n")
        .add_edge("n", END)
        .compile(checkpointer=rt.checkpointer)
    )
