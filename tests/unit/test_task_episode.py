"""Recovery projection uses actual SQLite ledger rows; no provider or ERP calls."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from db.models import AgentRun, HumanApproval, ToolSideEffect
from runtime.bootstrap import invoke_graph_with_resume
from runtime.context import reset_run_context, set_run_context
from runtime.task_episode import build_task_episode, safe_episode_summary
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory


@pytest.fixture
def ledger():
    factory, engine, path = make_sqlite_session_factory()
    session = factory()
    try:
        session.add(AgentRun(id="run", thread_id="thread", session_id="thread", status="RUNNING",
                             query="private customer question", attempt=2))
        session.add(AgentRun(id="foreign", thread_id="foreign-thread", session_id="foreign-thread",
                             status="RUNNING", query="foreign private question"))
        session.add_all([
            ToolSideEffect(run_id="run", thread_id="thread", tool_name="refund",
                           operation_key="op-1", request_fingerprint="a" * 64,
                           status="SUCCEEDED", result_reference={"secret": "private result"}),
            ToolSideEffect(run_id="run", thread_id="thread", tool_name="refund",
                           operation_key="op-2", request_fingerprint="b" * 64, status="PENDING"),
            ToolSideEffect(run_id="foreign", thread_id="foreign-thread", tool_name="refund",
                           operation_key="op-3", request_fingerprint="c" * 64, status="SUCCEEDED"),
            HumanApproval(approval_id="approval", run_id="run", thread_id="thread",
                          action="refund", risk_level="high", proposal={"secret": "private proposal"},
                          proposal_fingerprint="d" * 64, status="PENDING",
                          expires_at=datetime.now(timezone.utc) - timedelta(seconds=5)),
        ])
        session.commit()
        yield factory
    finally:
        session.close()
        dispose(engine, path)


def test_projection_uses_ledger_and_invalidates_after_receipt_change(ledger):
    episode = build_task_episode("run", "thread", ("refund",), approval_resume=True, session_factory=ledger)
    assert episode is not None
    assert episode.attempt == 2
    assert len(episode.succeeded_receipt_ids) == 1
    assert len(episode.uncertain_receipt_ids) == 1
    assert episode.approval_states == ("EXPIRED",)
    assert "private" not in str(safe_episode_summary(episode))
    assert build_task_episode("run", "foreign-thread", (), approval_resume=False, session_factory=ledger) is None
    session = ledger()
    try:
        row = session.get(ToolSideEffect, episode.uncertain_receipt_ids[0])
        row.status = "SUCCEEDED"
        session.commit()
    finally:
        session.close()
    updated = build_task_episode("run", "thread", ("refund",), approval_resume=True, session_factory=ledger)
    assert updated is not None
    assert updated.watermark != episode.watermark
    assert updated.uncertain_receipt_ids == ()


@pytest.mark.asyncio
async def test_actual_recovery_path_observes_projection_without_replaying_start(monkeypatch, ledger):
    monkeypatch.setenv("CSAI_TASK_EPISODE_ENABLED", "true")
    loader = Mock(side_effect=lambda *args, **kwargs: build_task_episode(*args, **kwargs, session_factory=ledger))
    monkeypatch.setattr("runtime.task_episode.build_task_episode", loader)
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(next=("refund",))),
                            ainvoke=AsyncMock(return_value={"response": "resumed"}))
    tokens = set_run_context("run", "thread")
    try:
        result, resumed = await invoke_graph_with_resume(graph, {"query": "must not restart"},
                                                         {"configurable": {"thread_id": "thread"}})
    finally:
        reset_run_context(tokens)
    assert resumed and result["response"] == "resumed"
    loader.assert_called_once_with("run", "thread", ("refund",), approval_resume=False)
    graph.ainvoke.assert_awaited_once_with(None, config={"configurable": {"thread_id": "thread"}})
