"""高风险操作的人工审批服务（durable）。

状态机：PENDING -> APPROVED | REJECTED | EXPIRED（终态）。
- ``create_or_get`` 按 (run_id, action, proposal_fingerprint) 幂等复用；
- ``decide`` 幂等：已决策的审批再次决策只返回既有记录（``newly_decided=False``）；
- 审批只是控制边界；approve 后仍由 Tool idempotency 保证副作用不重复。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.logger import get_logger
from db.database import get_db_session
from db.models import HumanApproval

logger = get_logger("core.hitl")

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_EXPIRED = "EXPIRED"
TERMINAL_STATUSES = frozenset({STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED})

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


class ApprovalNotFound(LookupError):
    """approval_id 不存在。"""


class InvalidApprovalDecision(ValueError):
    """非法审批决策。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _metric(name: str):
    try:
        from core import monitoring

        return getattr(monitoring, name, None)
    except Exception:
        return None


def _record_requested(risk_level: str, action: str) -> None:
    metric = _metric("human_approval_requested_total")
    if metric is not None:
        metric.labels(risk_level=risk_level, action=action).inc()
    pending = _metric("human_approval_pending")
    if pending is not None:
        pending.inc()


def _record_decided(decision: str, wait_seconds: float) -> None:
    metric = _metric("human_approval_decided_total")
    if metric is not None:
        metric.labels(decision=decision).inc()
    pending = _metric("human_approval_pending")
    if pending is not None:
        pending.dec()
    wait = _metric("human_approval_wait_seconds")
    if wait is not None:
        wait.observe(max(0.0, wait_seconds))


def _sanitize_proposal(proposal: dict[str, Any] | None) -> dict[str, Any]:
    from runtime.events import sanitize_event

    return sanitize_event(proposal or {})


def proposal_fingerprint(proposal: dict[str, Any] | None) -> str:
    payload = json.dumps(_sanitize_proposal(proposal), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _to_dict(row: HumanApproval) -> dict[str, Any]:
    return {
        "approval_id": row.approval_id,
        "run_id": row.run_id,
        "thread_id": row.thread_id,
        "user_id": row.user_id,
        "action": row.action,
        "risk_level": row.risk_level,
        "agent": row.agent,
        "proposal": row.proposal,
        "proposal_fingerprint": row.proposal_fingerprint,
        "status": row.status,
        "requested_at": row.requested_at,
        "reviewed_at": row.reviewed_at,
        "reviewer_id": row.reviewer_id,
        "decision": row.decision,
        "reason": row.reason,
        "resumed_at": row.resumed_at,
    }


class ApprovalService:
    def __init__(self, session_factory: Callable[[], Any] | None = None):
        self._session_factory = session_factory or get_db_session

    def _session(self):
        return self._session_factory()

    def create_or_get(
        self,
        *,
        run_id: str,
        thread_id: str,
        action: str,
        risk_level: str,
        proposal: dict[str, Any] | None,
        user_id: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """创建审批请求；同 (run_id, action, fingerprint) 已存在则返回既有记录。"""
        clean = _sanitize_proposal(proposal)
        fingerprint = proposal_fingerprint(clean)
        row = HumanApproval(
            approval_id=str(uuid.uuid4()),
            run_id=run_id,
            thread_id=thread_id,
            user_id=user_id,
            action=action,
            risk_level=risk_level,
            agent=agent,
            proposal=clean,
            proposal_fingerprint=fingerprint,
            status=STATUS_PENDING,
            requested_at=_utcnow(),
            updated_at=_utcnow(),
        )
        session = self._session()
        try:
            session.add(row)
            session.commit()
            session.refresh(row)
            record = _to_dict(row)
            _record_requested(risk_level, action)
            return record
        except IntegrityError:
            session.rollback()
        finally:
            session.close()

        existing = self._find_by_proposal(run_id, action, fingerprint)
        if existing is not None:
            return existing
        raise RuntimeError("human_approval 创建失败且未找到既有记录")

    def _find_by_proposal(
        self, run_id: str, action: str, fingerprint: str
    ) -> dict[str, Any] | None:
        session = self._session()
        try:
            row = session.execute(
                select(HumanApproval).where(
                    HumanApproval.run_id == run_id,
                    HumanApproval.action == action,
                    HumanApproval.proposal_fingerprint == fingerprint,
                )
            ).scalar_one_or_none()
            return _to_dict(row) if row is not None else None
        finally:
            session.close()

    def get(self, approval_id: str) -> dict[str, Any] | None:
        session = self._session()
        try:
            row = session.get(HumanApproval, approval_id)
            return _to_dict(row) if row is not None else None
        finally:
            session.close()

    def require(self, approval_id: str) -> dict[str, Any]:
        record = self.get(approval_id)
        if record is None:
            raise ApprovalNotFound(approval_id)
        return record

    def list_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._list_by_status(STATUS_PENDING, limit)

    def list_by_run(self, run_id: str) -> list[dict[str, Any]]:
        session = self._session()
        try:
            rows = (
                session.execute(
                    select(HumanApproval)
                    .where(HumanApproval.run_id == run_id)
                    .order_by(HumanApproval.requested_at.asc())
                )
                .scalars()
                .all()
            )
            return [_to_dict(r) for r in rows]
        finally:
            session.close()

    def _list_by_status(self, status: str, limit: int) -> list[dict[str, Any]]:
        session = self._session()
        try:
            rows = (
                session.execute(
                    select(HumanApproval)
                    .where(HumanApproval.status == status)
                    .order_by(HumanApproval.requested_at.asc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [_to_dict(r) for r in rows]
        finally:
            session.close()

    def decide(
        self,
        approval_id: str,
        *,
        reviewer_id: str,
        decision: str,
        reason: str | None = None,
        edited_args: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """决策审批。幂等：已终态则原样返回（newly_decided=False）。"""
        decision = (decision or "").strip().lower()
        if decision not in (DECISION_APPROVE, DECISION_REJECT):
            raise InvalidApprovalDecision(f"非法决策: {decision!r}")

        session = self._session()
        try:
            row = session.get(HumanApproval, approval_id)
            if row is None:
                raise ApprovalNotFound(approval_id)
            if row.status in TERMINAL_STATUSES:
                return _to_dict(row), False

            payload: dict[str, Any] = {"decision": decision}
            if decision == DECISION_APPROVE and edited_args:
                payload["edited_args"] = _sanitize_proposal(edited_args)
            if reason:
                payload["reason"] = reason[:2000]

            now = _utcnow()
            result = session.execute(
                update(HumanApproval)
                .where(
                    HumanApproval.approval_id == approval_id,
                    HumanApproval.status == STATUS_PENDING,
                )
                .values(
                    status=STATUS_APPROVED if decision == DECISION_APPROVE else STATUS_REJECTED,
                    reviewed_at=now,
                    reviewer_id=reviewer_id,
                    decision=payload,
                    reason=(reason[:2000] if reason else None),
                    updated_at=now,
                )
            )
            session.commit()
            if result.rowcount == 0:
                # 并发下已被决策
                session.refresh(row)
                return _to_dict(row), False
            session.refresh(row)
            record = _to_dict(row)
            reviewed = record.get("reviewed_at") or now
            requested = record.get("requested_at") or now
            _record_decided(decision, (reviewed - requested).total_seconds())
            return record, True
        finally:
            session.close()

    def get_resume_decision(self, run_id: str) -> dict[str, Any] | None:
        """返回该 run 待消费的已决策审批（用于 worker resume）。"""
        session = self._session()
        try:
            row = session.execute(
                select(HumanApproval)
                .where(
                    HumanApproval.run_id == run_id,
                    HumanApproval.status.in_([STATUS_APPROVED, STATUS_REJECTED]),
                    HumanApproval.resumed_at.is_(None),
                )
                .order_by(HumanApproval.reviewed_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            payload = dict(row.decision or {})
            payload["approval_id"] = row.approval_id
            payload["reviewer_id"] = row.reviewer_id
            return payload
        finally:
            session.close()

    def mark_resumed(self, approval_id: str) -> None:
        session = self._session()
        try:
            session.execute(
                update(HumanApproval)
                .where(HumanApproval.approval_id == approval_id)
                .values(resumed_at=_utcnow(), updated_at=_utcnow())
            )
            session.commit()
        finally:
            session.close()


_default_service: ApprovalService | None = None


def get_approval_service() -> ApprovalService:
    global _default_service
    if _default_service is None:
        _default_service = ApprovalService()
    return _default_service
