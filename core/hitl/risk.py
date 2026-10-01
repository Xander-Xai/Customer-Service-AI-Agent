"""Human-in-the-loop 风险策略。

只对真正高风险操作（退款/改单/高额赔付/投诉升级/ERP 写操作）拦截审批，
不给普通问答加审批。风险分级：

  LOW    只读查询（订单查询、产品信息）
  MEDIUM 低风险写（创建售后工单）—— 记录但不强制人工审批
  HIGH   退款 / 改单 / 高额赔付 / 投诉升级 / ERP 写操作 —— 必须人工审批
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_AMOUNT_FIELDS = ("amount", "refund_amount", "price", "total", "total_amount", "compensation")


def _parse_tool_list(raw: str) -> set[str]:
    return {t.strip().lower() for t in (raw or "").split(",") if t.strip()}


def _has_high_amount(arguments: dict[str, Any] | None, threshold: float) -> bool:
    if not arguments or threshold <= 0:
        return False
    for key in _AMOUNT_FIELDS:
        value = arguments.get(key)
        try:
            if value is not None and float(value) >= threshold:
                return True
        except (TypeError, ValueError):
            continue
    return False


def classify_risk(
    tool_name: str,
    *,
    explicit: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> RiskLevel:
    """对一次工具调用分级。

    优先级：工具显式 risk_level > 名称 allowlist > 金额阈值 > 默认 LOW。
    """
    if explicit:
        try:
            return RiskLevel(str(explicit).strip().lower())
        except ValueError:
            pass

    from core.config import (
        HITL_HIGH_AMOUNT_THRESHOLD,
        HITL_HIGH_RISK_TOOLS,
        HITL_MEDIUM_RISK_TOOLS,
    )

    name = (tool_name or "").strip().lower()
    if name in _parse_tool_list(HITL_HIGH_RISK_TOOLS):
        return RiskLevel.HIGH
    if _has_high_amount(arguments, HITL_HIGH_AMOUNT_THRESHOLD):
        return RiskLevel.HIGH
    if name in _parse_tool_list(HITL_MEDIUM_RISK_TOOLS):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def requires_approval(level: RiskLevel | str) -> bool:
    try:
        return RiskLevel(level) is RiskLevel.HIGH
    except ValueError:
        return False
