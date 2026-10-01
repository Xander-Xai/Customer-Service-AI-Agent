"""高风险操作的 LangGraph 风险闸门（interrupt / Command resume）。

流程（只在分布式 AgentRun 路径生效，需要 run_id 上下文）：

    Agent 工具循环发现 HIGH 风险调用
      -> 不直接执行，写入 state["pending_actions"]
    collaboration 节点之后 -> human_approval_gate 节点
      -> 创建 durable HumanApproval
      -> interrupt(payload)  -> Graph 暂停，checkpoint 落 PostgreSQL
    人工 approve/reject/edit（RBAC API）
      -> worker 用 Command(resume=decision) 恢复 Graph
      -> 闸门执行已批准动作（仍经 Tool idempotency），拒绝则不执行

注意：Python 3.10 + 当前 langgraph 版本不会自动把 runnable config 传入深层
async 节点，因此闸门节点显式设置 ``var_child_runnable_config``（已用真实
interrupt/Command 测试验证）。HITL 的目的是建立高风险 Tool 的业务控制边界，
不是展示 LangGraph API。
"""

from __future__ import annotations

from typing import Any

from core.logger import get_logger

logger = get_logger("core.hitl.gate")


def should_propose_approval(
    tool_name: str, arguments: dict[str, Any] | None, registry: Any = None
) -> bool:
    """仅分布式 run 路径 + HITL_ENABLED + HIGH 风险才转人工审批。"""
    from core.config import HITL_ENABLED

    if not HITL_ENABLED:
        return False
    from runtime.context import get_current_run_id

    if not get_current_run_id():
        return False
    from .risk import classify_risk, requires_approval

    explicit = None
    if registry is not None and hasattr(registry, "risk_level_for"):
        explicit = registry.risk_level_for(tool_name)
    return requires_approval(classify_risk(tool_name, explicit=explicit, arguments=arguments))


async def run_approval_gate(state: dict[str, Any], config: Any, container: Any) -> dict[str, Any]:
    """Graph 节点：对 pending_actions 逐个 interrupt，恢复后执行已批准动作。"""
    pending = list(state.get("pending_actions") or [])
    if not pending:
        return {}

    from langgraph.types import interrupt

    from runtime.context import get_current_run_id

    from .approval_service import get_approval_service

    run_id = get_current_run_id() or ""
    thread_id = state.get("session_id") or ""
    service = get_approval_service()

    # Python 3.10 workaround：显式提供 runnable config 上下文给 interrupt()
    from langchain_core.runnables.config import var_child_runnable_config

    token = var_child_runnable_config.set(config)
    try:
        for action in pending:
            record = service.create_or_get(
                run_id=run_id,
                thread_id=thread_id,
                action=action.get("tool", ""),
                risk_level=action.get("risk_level", "high"),
                proposal=action.get("arguments") or {},
                user_id=state.get("user_id"),
                agent=action.get("agent"),
            )
            decision = interrupt(
                {
                    "type": "human_approval_required",
                    "approval_id": record["approval_id"],
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "action": action.get("tool", ""),
                    "risk_level": action.get("risk_level", "high"),
                    "proposal": record["proposal"],
                    "allowed_decisions": ["approve", "reject", "edit"],
                }
            )
            action["_approval_id"] = record["approval_id"]
            action["_decision"] = decision or {}
    finally:
        var_child_runnable_config.reset(token)

    results = await execute_approved_actions(container, pending)
    return {"pending_actions": [], "approval_results": results}


async def execute_approved_actions(
    container: Any, pending: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """执行已批准动作；拒绝不执行；仍经 Tool idempotency。"""
    registry = getattr(container, "tool_registry", None)
    results: list[dict[str, Any]] = []
    for action in pending:
        decision = action.get("_decision") or {}
        verdict = str(decision.get("decision", "")).lower()
        tool_name = action.get("tool", "")
        if verdict != "approve":
            results.append(
                {"tool": tool_name, "status": "rejected", "reason": decision.get("reason", "")}
            )
            logger.info("[HITL] 拒绝执行: %s", tool_name)
            continue
        args = decision.get("edited_args") or action.get("arguments") or {}
        try:
            result = await registry.execute_raw(tool_name, args)
            results.append({"tool": tool_name, "status": "executed", "result": result})
            logger.info("[HITL] 已批准并执行: %s", tool_name)
        except Exception as e:
            results.append({"tool": tool_name, "status": "error", "error": type(e).__name__})
            logger.error("[HITL] 批准后执行失败: %s (%s)", tool_name, type(e).__name__)
    return results


__all__ = ["should_propose_approval", "run_approval_gate", "execute_approved_actions"]
