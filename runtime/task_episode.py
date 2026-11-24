"""Read-only, server-owned task handoff projection. Never an authorization token."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from db.models import AgentRun, HumanApproval, ToolSideEffect

from .statuses import TERMINAL_STATUSES, RunStatus

MAX_RECEIPTS = 50
_TERMINAL = {status.value for status in TERMINAL_STATUSES}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _utc(value)
    return normalized.isoformat() if normalized is not None else None


def read_task_episode(
    session: Any, run_id: str, *, now: datetime | None = None, limit: int = MAX_RECEIPTS
) -> dict[str, Any] | None:
    """Read current durable references; no query/result/proposal/args or error text.

    The caller must be a trusted worker or operator. No API or LLM access.
    Separate SELECTs are a diagnostic view, not an atomic authorization snapshot.
    Truncation, ambiguous writes and inconsistent identities require reconciliation.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RECEIPTS:
        raise ValueError("Invalid episode receipt limit")
    observed = _utc(now) or datetime.now(timezone.utc)
    run = session.execute(
        select(
            AgentRun.id,
            AgentRun.thread_id,
            AgentRun.user_id,
            AgentRun.status,
            AgentRun.attempt,
            AgentRun.max_attempts,
            AgentRun.task_id,
            AgentRun.updated_at,
        ).where(AgentRun.id == run_id)
    ).one_or_none()
    if run is None:
        return None
    approval_rows = session.execute(
        select(
            HumanApproval.approval_id,
            HumanApproval.thread_id,
            HumanApproval.user_id,
            HumanApproval.status,
            HumanApproval.expires_at,
            HumanApproval.resumed_at,
            HumanApproval.updated_at,
        )
        .where(HumanApproval.run_id == run_id)
        .order_by(HumanApproval.approval_id)
        .limit(limit + 1)
    ).all()
    receipt_rows = session.execute(
        select(
            ToolSideEffect.id,
            ToolSideEffect.thread_id,
            ToolSideEffect.status,
            ToolSideEffect.updated_at,
        )
        .where(ToolSideEffect.run_id == run_id)
        .order_by(ToolSideEffect.id)
        .limit(limit + 1)
    ).all()
    truncated = len(approval_rows) > limit or len(receipt_rows) > limit
    identity_mismatch = False
    approvals = []
    receipts = []
    for row in approval_rows[:limit]:
        if row.thread_id != run.thread_id or row.user_id != run.user_id:
            identity_mismatch = True
            continue
        expires = _utc(row.expires_at)
        known_statuses = {"PENDING", "APPROVED", "REJECTED", "EXPIRED"}
        status = row.status if row.status in known_statuses else "UNKNOWN"
        if status == "PENDING" and expires is not None and expires <= observed:
            status = "EXPIRED_PENDING_RECONCILIATION"
        approvals.append(
            {
                "approval_id": row.approval_id,
                "status": status,
                "consumed": row.resumed_at is not None,
                "expires_at": _iso(row.expires_at),
                "updated_at": _iso(row.updated_at),
            }
        )
    for row in receipt_rows[:limit]:
        if row.thread_id != run.thread_id:
            identity_mismatch = True
            continue
        status = row.status if row.status in {"PENDING", "SUCCEEDED", "FAILED"} else "UNKNOWN"
        receipts.append(
            {"receipt_id": row.id, "status": status, "updated_at": _iso(row.updated_at)}
        )

    if truncated or identity_mismatch:
        next_action = "inspect_complete_ledger"
    elif any(row["status"] != "SUCCEEDED" for row in receipts):
        next_action = "reconcile_write_receipts"
    elif any(row["status"] in {"UNKNOWN", "EXPIRED_PENDING_RECONCILIATION"} for row in approvals):
        next_action = "inspect_approval_ledger"
    elif run.status in _TERMINAL:
        next_action = "inspect_terminal_run"
    elif any(row["status"] == "PENDING" for row in approvals):
        next_action = "wait_for_approval"
    elif run.status == RunStatus.WAITING_APPROVAL.value:
        next_action = "inspect_approval_resume"
    else:
        next_action = "inspect_checkpoint"
    episode = {
        "schema_version": "task-episode/v1",
        "run_id": run.id,
        "thread_id": run.thread_id,
        "task_id": run.task_id,
        "objective_ref": {"source": "agent_runs", "run_id": run.id},
        "attempt": run.attempt,
        "max_attempts": run.max_attempts,
        "run_status": run.status,
        "run_updated_at": _iso(run.updated_at),
        "observed_at": observed.isoformat(),
        "approvals": approvals,
        "receipts": receipts,
        "truncated": truncated,
        "identity_mismatch": identity_mismatch,
        "next_safe_inspection": next_action,
        "execution_authorized": False,
    }

    # The digest represents only the bounded observed rows, not omitted rows or
    # a database snapshot. Exclude observation time so unchanged evidence has a
    # stable fingerprint, but include effective expiry/receipt status.
    fingerprint = {key: value for key, value in episode.items() if key != "observed_at"}
    episode["watermark"] = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return episode
