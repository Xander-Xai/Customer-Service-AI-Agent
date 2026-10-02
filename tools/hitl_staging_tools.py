"""Human-in-the-loop 治理验证用的**确定性 staging 工具**。

为什么需要它
------------
本仓库**没有**企业 staging 环境，也没有真实 ERP 写权限。因此「退款 / 改单」
这类 HIGH 风险副作用在本 PR 中**没有**、也**不应**被声称为已对真实 ERP 验证
过。为了仍然能**真实验证治理机制**（而不是靠 mock 断言 LangGraph API 的行为），
这里提供一个确定性的本地 staging 实现：

- 状态落在进程内的 staging 账本（``_STAGING_LEDGER``），可被测试直接断言
  「这一笔副作用到底被执行了几次」；
- 无随机性、无时间依赖、无外部 I/O，同样的输入永远得到同样的输出；
- 明确 ``side_effect=True`` + ``risk_level="high"``，因此**会**被审批闸门
  拦下——这正是要验证的路径。

边界（务必读）
--------------
1. 这些工具**不接**任何真实 ERP / 金蝶接口，**不产生**任何真实业务影响。
2. 它们存在的唯一目的是让「approve -> 恰好执行一次」「reject -> 零次执行」
   「重复投递 -> 仍然一次」这些**治理属性**可被自动化测试证明。
3. 接入真实 ERP 仍需：下游接受 idempotency key 的写接口 + 企业 staging +
   对账流程。那是**另一个 PR** 的事，本 PR 不声称该能力。
"""

from __future__ import annotations

import threading
from typing import Any

from core.logger import get_logger
from tools.tool_registry import ToolCachePolicy, ToolRegistry

logger = get_logger("tools.hitl_staging")

_LOCK = threading.Lock()

#: staging 账本：``operation_hint -> {"calls": int, "arguments": [...]}``
#:
#: 用 ``operation_hint``（业务单号）而非 ``tool_call_id`` 作为键，因为审批恢复
#: 会让 tool_call_id 变化，而「同一笔退款」必须按业务单号收敛。
_STAGING_LEDGER: dict[str, dict[str, Any]] = {}


def reset_staging_ledger() -> None:
    """清空 staging 账本（测试隔离用）。"""
    with _LOCK:
        _STAGING_LEDGER.clear()


def staging_ledger() -> dict[str, dict[str, Any]]:
    """返回 staging 账本的浅拷贝（测试断言用）。"""
    with _LOCK:
        return {k: dict(v) for k, v in _STAGING_LEDGER.items()}


def staging_call_count(operation_hint: str) -> int:
    """某个业务单号被**真正执行**的次数（= 副作用发生次数）。"""
    with _LOCK:
        entry = _STAGING_LEDGER.get(operation_hint)
        return int(entry["calls"]) if entry else 0


def _record(operation_hint: str, arguments: dict[str, Any]) -> None:
    with _LOCK:
        entry = _STAGING_LEDGER.setdefault(
            operation_hint, {"calls": 0, "arguments": [], "last": None}
        )
        entry["calls"] += 1
        entry["arguments"].append(dict(arguments))
        entry["last"] = dict(arguments)


async def _staging_refund(arguments: dict[str, Any]) -> str:
    """确定性 staging 退款。**不触碰真实支付/ERP。**"""
    order_id = str(arguments.get("order_id") or "")
    amount = arguments.get("amount")
    if not order_id:
        return "错误：缺少 order_id"
    try:
        amount_value = float(amount) if amount is not None else 0.0
    except (TypeError, ValueError):
        return "错误：amount 必须是数字"
    if amount_value <= 0:
        return "错误：amount 必须大于 0"

    _record(order_id, arguments)
    logger.info(
        "[staging] 执行退款 order_id=%s amount=%s（第 %d 次真实执行）",
        order_id,
        amount_value,
        staging_call_count(order_id),
    )
    return f"staging-refund-ok order_id={order_id} amount={amount_value}"


async def _staging_order_change(arguments: dict[str, Any]) -> str:
    """确定性 staging 改单。**不触碰真实 ERP。**"""
    order_id = str(arguments.get("order_id") or "")
    new_status = str(arguments.get("new_status") or "")
    if not order_id:
        return "错误：缺少 order_id"
    if not new_status:
        return "错误：缺少 new_status"

    hint = f"{order_id}:{new_status}"
    _record(hint, arguments)
    logger.info(
        "[staging] 执行改单 order_id=%s new_status=%s（第 %d 次真实执行）",
        order_id,
        new_status,
        staging_call_count(hint),
    )
    return f"staging-order-change-ok order_id={order_id} new_status={new_status}"


async def _staging_readonly_lookup(arguments: dict[str, Any]) -> str:
    """只读工具：``side_effect=False`` / ``risk_level="low"``，**不得**被闸门拦。"""
    order_id = str(arguments.get("order_id") or "")
    return f"staging-order-info order_id={order_id or 'unknown'} status=SHIPPED"


def register_hitl_staging_tools(registry: ToolRegistry) -> ToolRegistry:
    """注册 staging 治理验证工具（high / high / low 三档）。

    只在 HITL 显式开启且需要验证治理链路时注册；生产 ERP 工具不受影响。
    """
    registry.register(
        name="staging_refund",
        description=(
            "【staging 验证用】对订单发起退款。确定性本地实现，不接真实支付/ERP。"
            "用于验证高风险副作用的人工审批治理链路。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "amount": {"type": "number", "description": "退款金额"},
            },
            "required": ["order_id"],
        },
        handler=_staging_refund,
        side_effect=True,
        risk_level="high",
        cache_policy=ToolCachePolicy(enabled=False),
    )

    registry.register(
        name="staging_order_change",
        description=("【staging 验证用】修改订单状态。确定性本地实现，不接真实 ERP。"),
        parameters={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "new_status": {"type": "string", "description": "目标状态"},
            },
            "required": ["order_id", "new_status"],
        },
        handler=_staging_order_change,
        side_effect=True,
        risk_level="high",
        cache_policy=ToolCachePolicy(enabled=False),
    )

    registry.register(
        name="staging_readonly_lookup",
        description="【staging 验证用】只读订单查询。LOW 风险，不应被审批闸门拦截。",
        parameters={
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号"}},
            "required": ["order_id"],
        },
        handler=_staging_readonly_lookup,
        side_effect=False,
        risk_level="low",
        cache_policy=ToolCachePolicy(enabled=False),
    )

    logger.info("HITL staging 治理工具注册完成（不接真实 ERP）")
    return registry


__all__ = [
    "register_hitl_staging_tools",
    "reset_staging_ledger",
    "staging_call_count",
    "staging_ledger",
]
