"""Human-in-the-loop：高风险工具副作用的人工审批治理。

模块分层（各自可独立测试）：

- ``risk``            风险分级（LOW/MEDIUM/HIGH）；只有 HIGH 需要人工审批
- ``sanitize``        提案脱敏（审批留痕不得成为凭据泄漏通道）
- ``approval_service``durable 审批状态机 + TTL + 职责分离 + 幂等决策
- ``gate``            LangGraph interrupt / Command resume 闸门

调用方只需 ``should_propose_approval`` 与 ``get_approval_service``。
"""

from .approval_service import (
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_REJECT,
    STATUS_APPROVED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    TERMINAL_STATUSES,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalService,
    InvalidApprovalDecision,
    SelfApprovalForbidden,
    get_approval_service,
    proposal_fingerprint,
    reset_approval_service_for_tests,
)
from .gate import (
    collect_pending_actions,
    execute_approved_actions,
    has_pending_interrupt,
    proposal_to_pending_action,
    run_approval_gate,
    should_propose_approval,
)
from .risk import RiskLevel, classify_risk, requires_approval
from .sanitize import REDACTED, sanitize_proposal, sanitize_text

__all__ = [
    "DECISION_APPROVE",
    "DECISION_EDIT",
    "DECISION_REJECT",
    "REDACTED",
    "STATUS_APPROVED",
    "STATUS_EXPIRED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "TERMINAL_STATUSES",
    "ApprovalExpired",
    "ApprovalNotFound",
    "ApprovalService",
    "InvalidApprovalDecision",
    "RiskLevel",
    "SelfApprovalForbidden",
    "classify_risk",
    "collect_pending_actions",
    "execute_approved_actions",
    "get_approval_service",
    "has_pending_interrupt",
    "proposal_fingerprint",
    "proposal_to_pending_action",
    "requires_approval",
    "reset_approval_service_for_tests",
    "run_approval_gate",
    "sanitize_proposal",
    "sanitize_text",
    "should_propose_approval",
]
