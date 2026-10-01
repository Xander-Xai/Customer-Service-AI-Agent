"""Human-in-the-loop（高风险操作人工审批）。

- ``risk``：LOW/MEDIUM/HIGH 风险策略；
- ``approval_service``：durable 审批记录 + 状态机；
- ``gate``：LangGraph interrupt/Command 风险闸门。

目的不是展示 LangGraph API，而是建立高风险 Tool（退款/改单/高额赔付/投诉升级/
ERP 写操作）的业务控制边界。
"""

from .approval_service import (
    ApprovalService,
    get_approval_service,
)
from .risk import RiskLevel, classify_risk, requires_approval

__all__ = [
    "ApprovalService",
    "get_approval_service",
    "RiskLevel",
    "classify_risk",
    "requires_approval",
]
