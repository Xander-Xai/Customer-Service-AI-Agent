"""Bounded recovery episode from durable run and side-effect records.

This is diagnostic evidence, not model memory or permission to execute.
The source tables remain authoritative. No free text, query, result payload,
approval proposal, or tool arguments are copied into the projection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from db.database import get_db_session
from db.models import AgentRun, HumanApproval, ToolSideEffect

_MAX_ROWS = 32


@dataclass(frozen=True)
class TaskEpisode:
    schema_version: int
    run_id: str
    thread_id: str
    attempt: int
    resume_kind: str
    pending_step_count: int
    succeeded_receipt_ids: tuple[int, ...]
    uncertain_receipt_ids: tuple[int, ...]
    approval_states: tuple[str, ...]
    truncated: bool
    watermark: str
    next_safe_action: str = "consult_checkpoint_and_ledger"


def _effective_approval(status: str, expiry: datetime | None, now: datetime) -> str:
    if status == "PENDING" and expiry is not None:
        expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
        if expiry <= now:
            return "EXPIRED"
    return status if status in {"PENDING", "APPROVED", "REJECTED", "EXPIRED"} else "UNKNOWN"


def build_task_episode(
    run_id: str,
    thread_id: str,
    pending: tuple[str, ...],
    *,
    approval_resume: bool,
    session_factory: Callable[[], Any] = get_db_session,
    now: datetime | None = None,
) -> TaskEpisode | None:
    """Rebuild on each resume; never infer a successful external write from text.

    The watermark identifies the bounded observed projection only. Separate SQL
    reads are not an atomic snapshot, and truncated projections cannot establish
    freshness of omitted rows. Never use it as a fencing or authorization token.
    """
    session = session_factory()
    try:
        run = session.execute(
            select(AgentRun.id, AgentRun.thread_id, AgentRun.attempt, AgentRun.status).where(
                AgentRun.id == run_id, AgentRun.thread_id == thread_id
            )
        ).one_or_none()
        if run is None or run.status != "RUNNING":
            return None
        receipts = session.execute(
            select(ToolSideEffect.id, ToolSideEffect.status, ToolSideEffect.updated_at)
            .where(ToolSideEffect.run_id == run_id, ToolSideEffect.thread_id == thread_id)
            .order_by(ToolSideEffect.id)
            .limit(_MAX_ROWS + 1)
        ).all()
        approvals = session.execute(
            select(
                HumanApproval.approval_id,
                HumanApproval.status,
                HumanApproval.expires_at,
                HumanApproval.updated_at,
                HumanApproval.resumed_at,
            )
            .where(HumanApproval.run_id == run_id, HumanApproval.thread_id == thread_id)
            .order_by(HumanApproval.approval_id)
            .limit(_MAX_ROWS + 1)
        ).all()
        truncated = (
            len(receipts) > _MAX_ROWS or len(approvals) > _MAX_ROWS or len(pending) > _MAX_ROWS
        )
        receipts = receipts[:_MAX_ROWS]
        approvals = approvals[:_MAX_ROWS]
        current_time = now or datetime.now(timezone.utc)
        states = tuple(_effective_approval(row.status, row.expires_at, current_time) for row in approvals)
        fingerprint = {
            "run": [run_id, thread_id, run.attempt, run.status],
            "mode": "approval" if approval_resume else "checkpoint",
            "pending": pending[:_MAX_ROWS],
            "receipts": [(row.id, row.status, str(row.updated_at)) for row in receipts],
            "approvals": [
                (row.approval_id, state, str(row.updated_at), str(row.resumed_at))
                for row, state in zip(approvals, states, strict=True)
            ],
            "truncated": truncated,
        }
        watermark = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
        return TaskEpisode(
            schema_version=1,
            run_id=run_id,
            thread_id=thread_id,
            attempt=run.attempt,
            resume_kind=fingerprint["mode"],
            pending_step_count=min(len(pending), _MAX_ROWS),
            succeeded_receipt_ids=tuple(row.id for row in receipts if row.status == "SUCCEEDED"),
            uncertain_receipt_ids=tuple(row.id for row in receipts if row.status != "SUCCEEDED"),
            approval_states=states,
            truncated=truncated,
            watermark=watermark,
        )
    finally:
        session.close()


def safe_episode_summary(episode: TaskEpisode) -> dict[str, Any]:
    """Safe structured metadata for internal recovery telemetry only."""
    return asdict(episode)
