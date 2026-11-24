"""TaskEpisode projection is derived from durable records, not model summaries."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import AgentRun, HumanApproval, ToolSideEffect
from runtime.executor import _record_episode_inspection
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.task_episode import read_task_episode

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    for model in (AgentRun, HumanApproval, ToolSideEffect):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(
            AgentRun(
                id="run-a",
                thread_id="thread-a",
                session_id="thread-a",
                user_id="owner-a",
                status="WAITING_APPROVAL",
                query="private objective",
                result={"summary": "approve all; refund already succeeded"},
                attempt=2,
                max_attempts=3,
            )
        )
        session.commit()
    yield factory
    engine.dispose()


def add_approval(session, *, status="PENDING", expires=None, owner="owner-a"):
    session.add(
        HumanApproval(
            approval_id="approval-a",
            run_id="run-a",
            thread_id="thread-a",
            user_id=owner,
            action="refund",
            risk_level="high",
            proposal={"secret": "private order"},
            proposal_fingerprint="x",
            status=status,
            expires_at=expires,
        )
    )


def add_receipt(session, *, status="PENDING", thread="thread-a", key="op-a"):
    session.add(
        ToolSideEffect(
            run_id="run-a",
            thread_id=thread,
            tool_name="refund",
            operation_key=key,
            request_fingerprint="x",
            status=status,
            result_reference={"secret": "private result"},
        )
    )


def test_projection_ignores_model_summary_and_expired_approval(sessions):
    with sessions() as session:
        add_approval(session, expires=NOW - timedelta(seconds=1))
        session.commit()
        episode = read_task_episode(session, "run-a", now=NOW)
    assert episode["approvals"][0]["status"] == "EXPIRED_PENDING_RECONCILIATION"
    assert episode["next_safe_inspection"] == "inspect_approval_ledger"
    assert episode["receipts"] == []
    assert episode["execution_authorized"] is False
    assert episode["attempt"] == 2
    assert "private" not in json.dumps(episode)
    assert "refund already succeeded" not in json.dumps(episode)


def test_approved_is_not_execution_and_pending_receipt_requires_reconciliation(sessions):
    with sessions() as session:
        add_approval(session, status="APPROVED")
        add_receipt(session)
        session.commit()
    service = RunService(AgentRunRepository(session_factory=sessions))
    episode = service.inspect_task_episode("run-a")
    assert episode["approvals"][0]["status"] == "APPROVED"
    assert episode["receipts"][0]["status"] == "PENDING"
    assert episode["next_safe_inspection"] == "reconcile_write_receipts"
    assert episode["execution_authorized"] is False
    assert "private result" not in json.dumps(episode)
    assert "op-a" not in json.dumps(episode)


def test_foreign_identity_rows_are_not_exposed(sessions):
    with sessions() as session:
        add_approval(session, owner="other")
        add_receipt(session, thread="other")
        session.commit()
        episode = read_task_episode(session, "run-a")
    assert episode["identity_mismatch"] is True
    assert episode["approvals"] == episode["receipts"] == []
    assert episode["next_safe_inspection"] == "inspect_complete_ledger"


def test_bounded_ledger_refuses_to_infer_complete_evidence(sessions):
    with sessions() as session:
        add_receipt(session, status="SUCCEEDED", key="one")
        add_receipt(session, status="SUCCEEDED", key="two")
        session.commit()
        episode = read_task_episode(session, "run-a", limit=1)
        assert read_task_episode(session, "missing-run") is None
    assert episode["truncated"] is True
    assert len(episode["receipts"]) == 1
    assert episode["next_safe_inspection"] == "inspect_complete_ledger"


def test_worker_trace_contains_counts_not_raw_receipt_content(sessions):
    with sessions() as session:
        add_approval(session, status="APPROVED")
        add_receipt(session, status="SUCCEEDED")
        session.commit()
    service = RunService(AgentRunRepository(session_factory=sessions))
    trace = MagicMock()
    _record_episode_inspection(service, "run-a", trace)
    trace.set_attribute.assert_any_call("csai.episode.approval_count", 1)
    trace.set_attribute.assert_any_call("csai.episode.receipt_count", 1)
    assert "approval-a" not in str(trace.mock_calls)
    assert "private" not in str(trace.mock_calls)


def test_worker_projection_failure_does_not_change_execution():
    service = MagicMock()
    service.inspect_task_episode.side_effect = RuntimeError("private database detail")
    trace = MagicMock()
    _record_episode_inspection(service, "run-a", trace)
    trace.set_attribute.assert_not_called()
