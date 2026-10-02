"""高风险操作的人工审批服务（durable，PostgreSQL/SQLite 为真相源）。

状态机：``PENDING -> APPROVED | REJECTED | EXPIRED``（三者皆终态）。

职责边界
--------
审批**只回答「是否允许执行」**，绝不自己执行副作用。执行仍由
``runtime/side_effects.py`` 的幂等 ledger 兜底。二者互补：
审批防「不该做的被做了」，ledger 防「做了一次被重做」。

幂等契约
--------
- ``create_or_get``：同 ``(run_id, action, proposal_fingerprint)`` 复用既有记录，
  依赖数据库唯一约束 ``uq_human_approvals_proposal``（不靠 SELECT-then-INSERT）。
- ``decide``：条件更新 ``WHERE status='PENDING'``，并发双决策只有一方
  ``rowcount==1``；另一方读到既有终态并返回 ``newly_decided=False``。
- ``consume_resume``：``WHERE resumed_at IS NULL``，保证图恢复只消费一次决策，
  worker 重投递不会重复执行已批准的副作用。

安全契约
--------
- ``reviewer_id`` 必须 != ``user_id``（发起人）。在**本层**强制而非只在 API：
  任何绕过 HTTP 的调用方（脚本、内部任务）都受同一约束。
- 提案与理由落库前经 ``sanitize_proposal`` / ``sanitize_text`` 脱敏定长。
- TTL 到期按拒绝处理（``EXPIRED``），绝不「默认放行」。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.logger import get_logger
from db.database import get_db_session
from db.models import HumanApproval

from .sanitize import sanitize_proposal, sanitize_text

logger = get_logger("core.hitl.approval")

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_EXPIRED = "EXPIRED"
TERMINAL_STATUSES = frozenset({STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED})

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_EDIT = "edit"
#: 允许的决策取值。``edit`` 是「批准但替换参数」，终态仍是 APPROVED。
ALLOWED_DECISIONS = frozenset({DECISION_APPROVE, DECISION_REJECT, DECISION_EDIT})


class ApprovalNotFound(LookupError):
    """approval_id 不存在。"""


class InvalidApprovalDecision(ValueError):
    """非法审批决策（未知取值 / 空理由等）。"""


class SelfApprovalForbidden(PermissionError):
    """审批人 == 发起人，违反职责分离。"""


class ApprovalExpired(ValueError):
    """审批已过期，不能再决策。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """把 naive datetime 视为 UTC（SQLite 读回可能丢 tzinfo）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def default_ttl_seconds() -> float:
    try:
        from core.config import HITL_APPROVAL_TTL_SECONDS

        return float(HITL_APPROVAL_TTL_SECONDS)
    except Exception:
        return 3600.0


def _metrics() -> Any:
    """延迟导入 runtime.metrics（避免 core.hitl -> runtime 的导入环）。"""
    try:
        from runtime import metrics as run_metrics

        return run_metrics
    except Exception:  # pragma: no cover - metrics 模块不可用时静默
        return None


def _record_requested(risk_level: str) -> None:
    m = _metrics()
    if m is not None:
        m.record_approval_requested(risk_level)


def _record_decided(decision: str, wait_seconds: float) -> None:
    m = _metrics()
    if m is not None:
        m.record_approval_decided(decision, wait_seconds)


def _record_expired() -> None:
    m = _metrics()
    if m is not None:
        m.record_approval_expired()


def proposal_fingerprint(proposal: dict[str, Any] | None) -> str:
    """提案指纹（脱敏后计算，保证同一「实质提案」稳定同一指纹）。"""
    payload = json.dumps(
        sanitize_proposal(proposal or {}),
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
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
        "expires_at": row.expires_at,
        "reviewed_at": row.reviewed_at,
        "reviewer_id": row.reviewer_id,
        "decision": row.decision,
        "reason": row.reason,
        "resumed_at": row.resumed_at,
    }


def is_expired(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    """该审批是否已过 TTL（``expires_at`` 为 None 表示未设 TTL，永不过期）。"""
    if record.get("status") in TERMINAL_STATUSES:
        return record.get("status") == STATUS_EXPIRED
    expires_at = _aware(record.get("expires_at"))
    if expires_at is None:
        return False
    return (now or _utcnow()) >= expires_at


class ApprovalService:
    """审批的持久化与状态机；无副作用执行逻辑。"""

    def __init__(self, session_factory: Callable[[], Any] | None = None):
        self._session_factory = session_factory or get_db_session

    def _session(self):
        return self._session_factory()

    # -- 读取 ---------------------------------------------------------------

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

    def list_by_status(
        self, status: str, limit: int = 100, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        session = self._session()
        try:
            rows = (
                session.execute(
                    select(HumanApproval)
                    .where(HumanApproval.status == status)
                    .order_by(HumanApproval.requested_at.asc())
                    .limit(max(1, min(limit, 500)))
                )
                .scalars()
                .all()
            )
            return [_to_dict(r) for r in rows]
        finally:
            session.close()

    def list_pending(
        self, limit: int = 100, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """待审批列表。**自动落 EXPIRED**：读路径顺带收敛超时项。

        这样即使没有任何人调用 expire 接口，审批人看到的列表也不会包含早已超时
        却仍显示 PENDING 的条目（否则会诱导审批人「补批」一个本该失效的动作）。
        """
        now = now or _utcnow()
        pending = self.list_by_status(STATUS_PENDING, limit)
        live: list[dict[str, Any]] = []
        for record in pending:
            if is_expired(record, now=now):
                self.expire(record["approval_id"], now=now)
                continue
            live.append(record)
        return live

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

    def pending_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """该 run 仍待人工决策的审批（未过期）。"""
        now = _utcnow()
        return [
            r
            for r in self.list_by_run(run_id)
            if r["status"] == STATUS_PENDING and not is_expired(r, now=now)
        ]

    # -- 写入 ---------------------------------------------------------------

    def create_or_get(
        self,
        *,
        run_id: str,
        action: str,
        risk_level: str,
        proposal: dict[str, Any] | None,
        thread_id: str | None = None,
        user_id: str | None = None,
        agent: str | None = None,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        """创建审批请求；同 (run_id, action, fingerprint) 已存在则返回既有记录。

        幂等靠唯一约束 + IntegrityError 收敛，不用 SELECT-then-INSERT（后者在
        两个 worker 同时为同一 run 建审批时会双写）。
        """
        clean = sanitize_proposal(proposal or {})
        fingerprint = proposal_fingerprint(clean)
        ttl = default_ttl_seconds() if ttl_seconds is None else max(0.0, ttl_seconds)
        requested_at = _utcnow()
        expires_at = requested_at + timedelta(seconds=ttl) if ttl > 0 else None

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
            requested_at=requested_at,
            expires_at=expires_at,
            updated_at=requested_at,
        )
        session = self._session()
        try:
            session.add(row)
            session.commit()
            session.refresh(row)
            record = _to_dict(row)
            _record_requested(risk_level)
            logger.info(
                "[HITL] 新建审批 approval=%s run=%s action=%s risk=%s",
                record["approval_id"],
                run_id,
                action,
                risk_level,
            )
            return record
        except IntegrityError:
            session.rollback()
        finally:
            session.close()

        existing = self._find_by_proposal(run_id, action, fingerprint)
        if existing is not None:
            return existing
        raise RuntimeError("human_approval 创建失败且未找到既有记录")

    def _guard_reviewer(self, record: dict[str, Any], reviewer_id: str) -> None:
        """职责分离：审批人不得是发起人。

        在 service 层强制（而非只在 API 层），使内部脚本/任务等非 HTTP 调用方
        同样受约束。``user_id`` 为空表示无已知发起人（如系统触发），此时不阻断
        ——真正的边界由 API 的 RBAC（customer 无审批权）承担。
        """
        requester = record.get("user_id")
        if requester and reviewer_id and str(requester) == str(reviewer_id):
            raise SelfApprovalForbidden(
                f"审批人不得是发起人: approval={record.get('approval_id')} user={requester}"
            )

    def decide(
        self,
        approval_id: str,
        *,
        reviewer_id: str,
        decision: str,
        reason: str | None = None,
        edited_args: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """决策审批。返回 ``(record, newly_decided)``；幂等。

        ``newly_decided=False`` 表示该审批此前已被决策（或已过期），本次调用不
        改变任何状态——重复提交（双击 / 重试 / 并发）都是安全的 no-op。
        """
        normalized = (decision or "").strip().lower()
        if normalized not in ALLOWED_DECISIONS:
            raise InvalidApprovalDecision(
                f"非法决策: {decision!r}（允许 {sorted(ALLOWED_DECISIONS)}）"
            )
        if normalized == DECISION_EDIT and not edited_args:
            raise InvalidApprovalDecision("edit 决策必须提供 edited_args")
        reviewer_id = (reviewer_id or "").strip()
        if not reviewer_id:
            raise InvalidApprovalDecision("缺少审批人标识（reviewer_id）")

        now = now or _utcnow()
        session = self._session()
        try:
            row = session.get(HumanApproval, approval_id)
            if row is None:
                raise ApprovalNotFound(approval_id)
            if row.status in TERMINAL_STATUSES:
                return _to_dict(row), False

            self._guard_reviewer(_to_dict(row), reviewer_id)

            expires_at = _aware(row.expires_at)
            if expires_at is not None and now >= expires_at:
                # 超时按拒绝收敛，并明确落 EXPIRED（可与「明确拒绝」区分统计）
                row.status = STATUS_EXPIRED
                row.reviewed_at = now
                row.reviewer_id = reviewer_id
                row.decision = {
                    "decision": "expired",
                    "reason": "approval TTL elapsed before a human decision",
                }
                row.updated_at = now
                session.commit()
                session.refresh(row)
                _record_expired()
                logger.warning(
                    "[HITL] 审批过期拒绝决策 approval=%s reviewer=%s",
                    approval_id,
                    reviewer_id,
                )
                raise ApprovalExpired(f"审批已过期: {approval_id}")

            payload: dict[str, Any] = {"decision": normalized}
            clean_reason = sanitize_text(reason)
            if clean_reason:
                payload["reason"] = clean_reason
            if edited_args:
                payload["edited_args"] = sanitize_proposal(edited_args)

            result = session.execute(
                update(HumanApproval)
                .where(
                    HumanApproval.approval_id == approval_id,
                    HumanApproval.status == STATUS_PENDING,
                )
                .values(
                    status=(
                        STATUS_APPROVED
                        if normalized in (DECISION_APPROVE, DECISION_EDIT)
                        else STATUS_REJECTED
                    ),
                    reviewed_at=now,
                    reviewer_id=reviewer_id,
                    decision=payload,
                    reason=clean_reason,
                    updated_at=now,
                )
            )
            session.commit()
            if result.rowcount == 0:
                # 并发下已被另一方决策：原样返回既有终态
                session.refresh(row)
                return _to_dict(row), False
            session.refresh(row)
            record = _to_dict(row)
            requested = _aware(record.get("requested_at")) or now
            _record_decided(normalized, (now - requested).total_seconds())
            logger.info(
                "[HITL] 审批已决策 approval=%s decision=%s reviewer=%s",
                approval_id,
                normalized,
                reviewer_id,
            )
            return record, True
        finally:
            session.close()

    def expire(self, approval_id: str, *, now: datetime | None = None) -> bool:
        """把已超时且仍 PENDING 的审批落为 EXPIRED。返回是否真的 transitioned。"""
        now = now or _utcnow()
        session = self._session()
        try:
            row = session.get(HumanApproval, approval_id)
            if row is None or row.status in TERMINAL_STATUSES:
                return False
            expires_at = _aware(row.expires_at)
            if expires_at is None or now < expires_at:
                return False
            result = session.execute(
                update(HumanApproval)
                .where(
                    HumanApproval.approval_id == approval_id,
                    HumanApproval.status == STATUS_PENDING,
                )
                .values(
                    status=STATUS_EXPIRED,
                    reviewed_at=now,
                    decision={"decision": "expired", "reason": "approval TTL elapsed"},
                    updated_at=now,
                )
            )
            session.commit()
            if result.rowcount == 0:
                return False
            _record_expired()
            logger.info("[HITL] 审批超时置为 EXPIRED approval=%s", approval_id)
            return True
        finally:
            session.close()

    # -- resume 消费（幂等） -------------------------------------------------

    def consume_resume(self, run_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        """原子地取出该 run 待消费的已决策审批并标记 ``resumed_at``。

        ``WHERE resumed_at IS NULL`` 保证**只被消费一次**：worker 崩溃后重投递
        不会重复执行已批准的副作用（与 side-effect ledger 构成双保险）。

        超时的 PENDING 审批在此被落 EXPIRED 并作为「拒绝」返回，避免图无限等待。
        """
        now = now or _utcnow()
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
                # 没有已决策的：把已超时的 PENDING 收敛成 EXPIRED，等价拒绝
                pending_row = session.execute(
                    select(HumanApproval)
                    .where(
                        HumanApproval.run_id == run_id,
                        HumanApproval.status == STATUS_PENDING,
                        HumanApproval.resumed_at.is_(None),
                    )
                    .order_by(HumanApproval.requested_at.asc())
                    .limit(1)
                ).scalar_one_or_none()
                if pending_row is None:
                    return None
                expires_at = _aware(pending_row.expires_at)
                if expires_at is None or now < expires_at:
                    return None  # 仍在有效等待期：不消费，图应继续挂起
                result = session.execute(
                    update(HumanApproval)
                    .where(
                        HumanApproval.approval_id == pending_row.approval_id,
                        HumanApproval.status == STATUS_PENDING,
                        HumanApproval.resumed_at.is_(None),
                    )
                    .values(
                        status=STATUS_EXPIRED,
                        reviewed_at=now,
                        decision={"decision": "expired", "reason": "approval TTL elapsed"},
                        resumed_at=now,
                        updated_at=now,
                    )
                )
                session.commit()
                if result.rowcount == 0:
                    return None
                _record_expired()
                logger.warning("[HITL] 恢复时发现审批已过期，按拒绝恢复 run=%s", run_id)
                # 上面的 UPDATE 已经**原子地**完成了认领：它的 WHERE 同时要求
                # ``status=PENDING`` 且 ``resumed_at IS NULL``，并在同一条语句里写下
                # ``resumed_at=now``。因此这里**不能**再走下面的 claim 分支——那个
                # 分支要求 ``resumed_at IS NULL``，而本行刚刚被自己置为非空，
                # rowcount 必然为 0，结果是过期审批永远无法被消费，图会一直挂在
                # interrupt 上（正是本分支要避免的死等）。直接构造 payload 返回。
                payload = dict(pending_row.decision or {})
                payload["approval_id"] = pending_row.approval_id
                payload["reviewer_id"] = pending_row.reviewer_id
                payload["action"] = pending_row.action
                payload["risk_level"] = pending_row.risk_level
                return payload

            claimed = session.execute(
                update(HumanApproval)
                .where(
                    HumanApproval.approval_id == row.approval_id,
                    HumanApproval.resumed_at.is_(None),
                )
                .values(resumed_at=now, updated_at=now)
            )
            session.commit()
            if claimed.rowcount == 0:
                return None  # 已被另一 worker 消费
            session.refresh(row)
            payload = dict(row.decision or {})
            payload["approval_id"] = row.approval_id
            payload["reviewer_id"] = row.reviewer_id
            payload["action"] = row.action
            payload["risk_level"] = row.risk_level
            return payload
        finally:
            session.close()

    def mark_resumed(self, approval_id: str, *, now: datetime | None = None) -> None:
        """显式标记已消费（兼容不需要 payload 的调用方）。"""
        now = now or _utcnow()
        session = self._session()
        try:
            session.execute(
                update(HumanApproval)
                .where(HumanApproval.approval_id == approval_id)
                .values(resumed_at=now, updated_at=now)
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


def reset_approval_service_for_tests() -> None:
    global _default_service
    _default_service = None


__all__ = [
    "ALLOWED_DECISIONS",
    "DECISION_APPROVE",
    "DECISION_EDIT",
    "DECISION_REJECT",
    "STATUS_APPROVED",
    "STATUS_EXPIRED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "TERMINAL_STATUSES",
    "ApprovalExpired",
    "ApprovalNotFound",
    "ApprovalService",
    "InvalidApprovalDecision",
    "SelfApprovalForbidden",
    "default_ttl_seconds",
    "get_approval_service",
    "is_expired",
    "proposal_fingerprint",
    "reset_approval_service_for_tests",
]
