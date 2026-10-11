"""Human-in-the-loop 风险策略。

只对真正高风险操作（退款/改单/高额赔付/投诉升级/ERP 写操作）拦截审批，
不给普通问答加审批。风险分级：

  LOW    只读查询（订单查询、产品信息）
  MEDIUM 低风险写（创建售后工单）—— 记录但不强制人工审批
  HIGH   退款 / 改单 / 高额赔付 / 投诉升级 / ERP 写操作 —— 必须人工审批

判定链（fail-closed）
--------------------
    工具显式 risk_level
      > HITL_HIGH_RISK_TOOLS 名称白名单
      > 金额阈值
      > HITL_MEDIUM_RISK_TOOLS 名称白名单
      > **未声明风险等级的有副作用工具 -> HIGH**
      > 默认 LOW

倒数第二条是历史 fail-open 的修复点。原来链条直接落到「默认 LOW」，于是：

    HITL_ENABLED=true
    HITL_HIGH_RISK_TOOLS=""        # 空
    HITL_HIGH_AMOUNT_THRESHOLD=0   # 关掉金额维度

会让**每一个**工具都判为 LOW，`requires_approval` 恒为 False —— 审批开关开着、
闸门却什么都不拦，而且没有任何告警。一个以「拦截高危写操作」为存在理由的治理
边界，在「配置写漏了」这种最常见的事故里变成了摆设。

现在的策略：``HITL_ENABLED=true`` 时，**任何声明了 ``side_effect=True`` 却没有被
上面任何一条规则覆盖**的工具，一律按 HIGH 处理（需要审批）。理由是运维显式打开了
审批开关，就说明他要治理副作用；此时一个「有副作用但没人认领风险等级」的工具属于
**覆盖缺口**，不是「有意放行」。有意放行有专门的表达方式 ——
把它写进 ``HITL_MEDIUM_RISK_TOOLS``。

只读工具完全不受这条影响（``side_effect=False``），因此普通问答 / 订单查询的
执行路径与延迟语义没有任何变化。

配套的启动校验在 ``core.config.validate_hitl_settings``：开了开关却没有任何规则
能命中 HIGH 时**拒绝启动**，而不是等到第一次高危调用才发现没人拦。
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
    side_effect: bool = False,
) -> RiskLevel:
    """对一次工具调用分级。

    优先级：工具显式 risk_level > HIGH 名称白名单 > 金额阈值 > MEDIUM 名称白名单
    > **未认领风险等级的有副作用工具 -> HIGH** > 默认 LOW。

    ``side_effect``
        该工具是否声明为写操作（来自 ``ToolRegistry.is_side_effect``）。仅在
        **没有任何其它规则命中**时才起作用，且只把结果从 LOW 抬到 HIGH；
        显式声明与白名单永远优先，因此运维的配置不会被这条默认值覆盖。

        留默认 ``False`` 是刻意的：调用方必须**显式**声明工具是否有副作用，
        不能因为「忘了传」就默认安全（那正是历史 fail-open 的形态）。
    """
    if explicit:
        try:
            return RiskLevel(str(explicit).strip().lower())
        except ValueError:
            pass

    from core.config import (
        HITL_ENABLED,
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
    if side_effect and HITL_ENABLED:
        # 覆盖缺口：有副作用、但没有任何规则认领它的风险等级。fail-closed。
        return RiskLevel.HIGH
    return RiskLevel.LOW


def requires_approval(level: RiskLevel | str) -> bool:
    """是否需要人工审批：**只有 HIGH**。

    大小写与空白归一化后再比较：``RiskLevel`` 的值是小写，若直接
    ``RiskLevel("HIGH")`` 会抛 ``ValueError`` 并落到 ``False``——那是**放行**
    方向。审批判定宁可多拦一次（人看一眼就过），不可因大小写笔误把高风险操作
    静默放行。归一化后与 :func:`classify_risk` 对 ``explicit`` 的处理保持一致。
    """
    if isinstance(level, RiskLevel):
        return level is RiskLevel.HIGH
    try:
        return RiskLevel(str(level).strip().lower()) is RiskLevel.HIGH
    except (ValueError, TypeError):
        return False


__all__ = ["RiskLevel", "classify_risk", "requires_approval"]
