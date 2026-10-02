"""PostgreSQL checkpointer 跨进程持久化集成测试。

仅在提供真实 PostgreSQL 时运行：
    TEST_POSTGRES_CHECKPOINT_URL=postgresql://user:pass@host:5432/db \\
        pytest tests/integration/test_checkpoint_postgres.py -q

未设置该环境变量时整个模块跳过（CI 无 PG 时不会误报）。
此测试证明：invoke graph -> checkpoint 落库 -> 销毁运行时（模拟进程退出）
-> 重建运行时/图 -> 同 thread_id 恢复；不同 thread 隔离。
"""

import os
import uuid

import pytest

CHECKPOINT_URL = os.getenv("TEST_POSTGRES_CHECKPOINT_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not CHECKPOINT_URL,
    reason="TEST_POSTGRES_CHECKPOINT_URL 未设置；需要真实 PostgreSQL 才能运行",
)


def _build_tiny_graph(checkpointer):
    from typing import TypedDict

    from langgraph.graph import StateGraph

    class S(TypedDict, total=False):
        value: int

    graph = StateGraph(S)
    graph.add_node("inc", lambda s: {"value": s.get("value", 0) + 1})
    graph.set_entry_point("inc")
    graph.set_finish_point("inc")
    return graph.compile(checkpointer=checkpointer)


async def test_checkpoint_survives_runtime_restart():
    from core.checkpointer import build_postgres_checkpointer, close_checkpoint_runtime

    thread_a = f"it-a-{uuid.uuid4().hex}"
    thread_b = f"it-b-{uuid.uuid4().hex}"
    config_a = {"configurable": {"thread_id": thread_a}}
    config_b = {"configurable": {"thread_id": thread_b}}

    runtime1 = await build_postgres_checkpointer(CHECKPOINT_URL, setup_timeout=10.0)
    try:
        graph1 = _build_tiny_graph(runtime1.checkpointer)
        result = await graph1.ainvoke({"value": 0}, config=config_a)
        assert result["value"] == 1
    finally:
        # 模拟进程退出：销毁 saver + 连接池
        await close_checkpoint_runtime(runtime1)

    # 全新运行时/图（新进程语义），复用同一 PostgreSQL
    runtime2 = await build_postgres_checkpointer(CHECKPOINT_URL, setup_timeout=10.0)
    try:
        graph2 = _build_tiny_graph(runtime2.checkpointer)
        state_a = await graph2.aget_state(config_a)
        assert state_a.values["value"] == 1

        state_b = await graph2.aget_state(config_b)
        assert state_b.values == {}

        result_b = await graph2.ainvoke({"value": 100}, config=config_b)
        assert result_b["value"] == 101
        assert (await graph2.aget_state(config_a)).values["value"] == 1

        # 清理测试 thread，避免表中累积
        await runtime2.checkpointer.adelete_thread(thread_a)
        await runtime2.checkpointer.adelete_thread(thread_b)
    finally:
        await close_checkpoint_runtime(runtime2)


async def test_checkpoint_visible_across_two_live_instances():
    """实例 A 写入 -> 同时存活的实例 B 读取同一 thread 状态（真跨实例）。"""
    from core.checkpointer import build_postgres_checkpointer, close_checkpoint_runtime

    thread = f"it-live-{uuid.uuid4().hex}"
    config = {"configurable": {"thread_id": thread}}

    runtime_a = await build_postgres_checkpointer(CHECKPOINT_URL, setup_timeout=10.0)
    runtime_b = await build_postgres_checkpointer(CHECKPOINT_URL, setup_timeout=10.0)
    try:
        graph_a = _build_tiny_graph(runtime_a.checkpointer)
        graph_b = _build_tiny_graph(runtime_b.checkpointer)

        await graph_a.ainvoke({"value": 41}, config=config)

        # B 与 A 同时存活，从共享 PostgreSQL 读到 A 写入的 checkpoint
        state_b = await graph_b.aget_state(config)
        assert state_b.values["value"] == 42

        # B 继续同 thread 执行，A 也能看到最新值
        result_b = await graph_b.ainvoke({"value": 42}, config=config)
        assert result_b["value"] == 43
        assert (await graph_a.aget_state(config)).values["value"] == 43
    finally:
        try:
            await runtime_a.checkpointer.adelete_thread(thread)
        finally:
            await close_checkpoint_runtime(runtime_a)
            await close_checkpoint_runtime(runtime_b)
