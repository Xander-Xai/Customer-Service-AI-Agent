"""Task episode recovery ownership; no model, Redis or external side effects."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runtime.bootstrap import (
    CheckpointEpisodeMismatch,
    inspect_episode_checkpoint,
    invoke_graph_with_resume,
)
from runtime.context import reset_run_context, set_run_context
from runtime.errors import TransientError

CONFIG = {"configurable": {"thread_id": "shared-thread"}}


def _graph(owner, pending=("human_approval_gate",)):
    return SimpleNamespace(
        aget_state=AsyncMock(
            return_value=SimpleNamespace(next=pending, values={"episode_run_id": owner})
        ),
        ainvoke=AsyncMock(return_value={"response": "test"}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["old-run", None])
@pytest.mark.parametrize("approval", [False, True])
async def test_worker_refuses_foreign_or_legacy_pending_checkpoint(owner, approval):
    graph = _graph(owner)
    tokens = set_run_context("new-run", "shared-thread")
    try:
        with pytest.raises(CheckpointEpisodeMismatch):
            await invoke_graph_with_resume(
                graph,
                {"episode_run_id": "new-run"},
                CONFIG,
                resume_command=object() if approval else None,
            )
        graph.ainvoke.assert_not_awaited()
    finally:
        reset_run_context(tokens)


@pytest.mark.asyncio
async def test_same_episode_resumes_without_new_input():
    graph = _graph("same-run")
    tokens = set_run_context("same-run", "shared-thread")
    try:
        result, resumed = await invoke_graph_with_resume(
            graph, {"episode_run_id": "same-run"}, CONFIG
        )
        assert resumed
        assert result == {"response": "test"}
        graph.ainvoke.assert_awaited_once_with(None, config=CONFIG)
    finally:
        reset_run_context(tokens)


@pytest.mark.asyncio
async def test_new_episode_can_follow_completed_checkpoint():
    graph = _graph("old-run", pending=())
    state = {"episode_run_id": "new-run"}
    tokens = set_run_context("new-run", "shared-thread")
    try:
        _, resumed = await invoke_graph_with_resume(graph, state, CONFIG)
        assert not resumed
        graph.ainvoke.assert_awaited_once_with(state, config=CONFIG)
    finally:
        reset_run_context(tokens)


@pytest.mark.asyncio
async def test_worker_does_not_start_over_on_checkpoint_backend_failure():
    graph = _graph("same-run")
    graph.aget_state.side_effect = OSError("private connection details")
    tokens = set_run_context("same-run", "shared-thread")
    try:
        with pytest.raises(TransientError, match="Cannot inspect worker checkpoint"):
            await invoke_graph_with_resume(graph, {}, CONFIG)
        graph.ainvoke.assert_not_awaited()
    finally:
        reset_run_context(tokens)


@pytest.mark.asyncio
async def test_sync_path_refuses_to_adopt_worker_episode():
    graph = _graph("worker-run")
    with pytest.raises(CheckpointEpisodeMismatch):
        await inspect_episode_checkpoint(graph, CONFIG, None)
    graph.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_cannot_resume_completed_episode():
    graph = _graph("same-run", pending=())
    tokens = set_run_context("same-run", "shared-thread")
    try:
        with pytest.raises(CheckpointEpisodeMismatch):
            await invoke_graph_with_resume(graph, {}, CONFIG, resume_command=object())
        graph.ainvoke.assert_not_awaited()
    finally:
        reset_run_context(tokens)


@pytest.mark.asyncio
async def test_real_memory_saver_preserves_episode_channel_and_rejects_other_run():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from core.state import AgentState

    builder = StateGraph(AgentState)
    builder.add_node("first", lambda state: {"response": "first"})
    builder.add_node("second", lambda state: {"response": "finished"})
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    graph = builder.compile(checkpointer=InMemorySaver(), interrupt_before=["second"])
    await graph.ainvoke(
        {"episode_run_id": "original-run", "customer_query": "synthetic"}, config=CONFIG
    )

    assert await inspect_episode_checkpoint(graph, CONFIG, "original-run") == ("second",)
    with pytest.raises(CheckpointEpisodeMismatch):
        await inspect_episode_checkpoint(graph, CONFIG, "another-run")
    tokens = set_run_context("original-run", "shared-thread")
    try:
        result, resumed = await invoke_graph_with_resume(graph, {}, CONFIG)
        assert resumed
        assert result["response"] == "finished"
        assert result["episode_run_id"] == "original-run"
    finally:
        reset_run_context(tokens)
