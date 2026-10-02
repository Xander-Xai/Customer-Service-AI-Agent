"""高风险工具副作用的人工审批闸门（LangGraph interrupt / Command resume）。

设计意图
--------
本模块**不是** LangGraph ``interrupt`` API 的演示。目标是给「高风险 Tool
Side Effect」建立一条企业可用的治理边界：

    Agent 工具循环判定 HIGH 风险
      -> 不执行，写入 state["pending_actions"]
      -> 本节点创建 durable HumanApproval（PostgreSQL）
      -> interrupt(payload) 挂起；checkpoint 落库，run 转入 WAITING_APPROVAL
    人工 approve / edit / reject（RBAC API，reviewer != requester）
      -> 审批记录落终态
      -> worker 以 Command(resume=decision) 恢复本节点
      -> 按决策执行（执行仍经 runtime.side_effects 幂等 ledger）

两条独立的防线，缺一不可
------------------------
1. **审批**：防「不该做的被做了」——未获批准不执行。
2. **幂等 ledger**：防「做了一次被重做」——worker 崩溃 / at-least-once 重投递后，
   同一 ``run_id:approval:{approval_id}`` 只真正触发一次外部写操作。

已验证的 LangGraph 语义（langgraph 1.2.12 / Python 3.10，本仓库实测）
--------------------------------------------------------------------
- **必须**显式设置 ``var_child_runnable_config``：该环境下 ``interrupt()``
  内部调用 ``get_config()`` 会抛
  ``RuntimeError: Called get_config outside of a runnable context``。
  不做 workaround 的节点 100% 无法收集到 interrupt。
- ``ainvoke(...)`` 在 interrupt 时**不抛异常**，而是在返回值里带
  ``__interrupt__``；同时 ``aget_state().next`` 非空（节点待重放）。
- **``ainvoke(None, config)`` 无法解除 interrupt**：实测它原样再次返回
  ``__interrupt__``、节点不推进。因此恢复必须显式传
  ``Command(resume=...)``；``runtime/bootstrap.py`` 里的
  ``invoke_graph_with_resume`` 的 ``ainvoke(None)`` 路径只适用于
  「崩溃恢复」而非「审批恢复」，两者必须区分。
- 恢复时节点从头重放：``interrupt()`` 之前的副作用会再跑一次。因此本节点
  在 interrupt 之前**只做无副作用的读操作**，创建审批走幂等 ``create_or_get``。
"""

from __future__ import annotations

from typing import Any, cast

from core.logger import get_logger

logger = get_logger("core.hitl.gate")

#: LangGraph 在 interrupt 发生时于返回值里注入的 key（实测 langgraph 1.2.12）。
INTERRUPT_KEY = "__interrupt__"


def _metrics() -> Any:
    try:
        from runtime import metrics as run_metrics

        return run_metrics
    except Exception:  # pragma: no cover
        return None


def should_propose_approval(
    tool_name: str,
    arguments: dict[str, Any] | None,
    registry: Any = None,
) -> bool:
    """是否需要人工审批：**仅**分布式 run 路径 + ``HITL_ENABLED`` + HIGH 风险。

    三重收敛，缺一不拦：
      - ``HITL_ENABLED`` 总开关；
      - ``get_current_run_id()``：只有异步 Run 路径有 durable checkpoint 与
        run 上下文；``/api/chat`` 实时快路径无 run 上下文，拦了也无法挂起/恢复，
        因此明确不在此拦（该路径的治理边界由部署形态决定，不由本模块假装覆盖）；
      - ``requires_approval(classify_risk(...))``：只有 HIGH。
    """
    try:
        from core.config import HITL_ENABLED
    except Exception:
        return False
    if not HITL_ENABLED:
        return False

    try:
        from runtime.context import get_current_run_id
    except Exception:
        return False
    if not get_current_run_id():
        return False

    from .risk import classify_risk, requires_approval

    explicit = None
    if registry is not None and hasattr(registry, "risk_level_for"):
        explicit = registry.risk_level_for(tool_name)
    return requires_approval(classify_risk(tool_name, explicit=explicit, arguments=arguments))


def proposal_to_pending_action(
    action: str, arguments: dict[str, Any] | None, **extra: Any
) -> dict[str, Any]:
    """构造 ``state["pending_actions"]`` 条目。"""
    payload: dict[str, Any] = {"tool": action, "arguments": arguments or {}}
    payload.update(extra)
    return payload


def collect_pending_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    pending = list(state.get("pending_actions") or [])
    return [p for p in pending if isinstance(p, dict) and p.get("tool")]


def has_pending_interrupt(result: Any) -> bool:
    """执行结果是否表示「图正挂在 interrupt 上等人工」。

    LangGraph 在 interrupt 时不抛异常，而是在返回值注入 ``__interrupt__``。
    executor 用它决定「标记 WAITING_APPROVAL」而不是「标记 SUCCEEDED」。
    """
    if not isinstance(result, dict):
        return False
    return bool(result.get(INTERRUPT_KEY))


async def run_approval_gate(state: dict[str, Any], config: Any, container: Any) -> dict[str, Any]:
    """图节点：对 pending_actions 逐个 interrupt，恢复后按决策执行。"""
    pending = collect_pending_actions(state)
    if not pending:
        return {}

    from langgraph.types import interrupt

    from runtime.context import get_current_run_id

    from .approval_service import get_approval_service

    run_id = get_current_run_id() or ""
    thread_id = state.get("session_id") or ""
    service = get_approval_service()

    # langgraph 1.2.12 + Python 3.10：interrupt() 依赖的 get_config() 在深层
    # async 节点里拿不到 runnable config，必须显式注入（见模块 docstring 的实测）。
    from langchain_core.runnables.config import (
        RunnableConfig,
        var_child_runnable_config,
    )

    runnable_config: RunnableConfig = cast(RunnableConfig, config) if config else {}
    token = var_child_runnable_config.set(runnable_config)
    try:
        for action in pending:
            # 幂等：worker 重投递 / 节点重放都复用同一条审批，不重复打扰审批人。
            record = service.create_or_get(
                run_id=run_id,
                thread_id=thread_id,
                action=str(action.get("tool") or ""),
                risk_level=str(action.get("risk_level") or "high"),
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
                    "action": record["action"],
                    "risk_level": record["risk_level"],
                    "proposal": record["proposal"],
                    "allowed_decisions": ["approve", "edit", "reject"],
                    "expires_at": record.get("expires_at"),
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
    """执行已批准的动作；拒绝/过期不执行；执行仍经 side-effect 幂等 ledger。

    ``tool_call_id`` 的选择是本函数的关键正确性点
    ---------------------------------------------
    ``ToolRegistry._idempotent_operation`` 要求同时具备 ``tool_call_id`` 与 run
    上下文才会走 ledger；缺 ``tool_call_id`` 会**静默降级为非幂等直调**。
    历史实现在这里调 ``execute_raw(name, args)`` 不传 ``tool_call_id``，于是
    「已批准的退款」在 worker 崩溃重投后会**再执行一次**——而 docstring 却声称
    「仍经 Tool idempotency」。这里改用审批 ID 派生确定性 call id：

        tool_call_id = "approval:{approval_id}"
        operation_key = f"{run_id}:approval:{approval_id}"

    它在同一次审批的任意次重试中恒定（审批幂等），因此 ledger 能正确去重；
    不同审批的 key 天然不同，不会误伤合法的第二次同类操作。
    """
    registry = getattr(container, "tool_registry", None)
    metrics = _metrics()
    results: list[dict[str, Any]] = []

    if registry is None:
        for action in pending:
            results.append(
                {"tool": action.get("tool"), "status": "error", "error": "ToolRegistryUnavailable"}
            )
        logger.error("[HITL] 容器内无 tool_registry，已批准动作无法执行")
        return results

    for action in pending:
        decision = action.get("_decision") or {}
        verdict = str(decision.get("decision", "")).strip().lower()
        tool_name = str(action.get("tool") or "")
        approval_id = str(action.get("_approval_id") or "")

        if verdict not in ("approve", "edit"):
            results.append(
                {
                    "tool": tool_name,
                    "approval_id": approval_id,
                    "status": "rejected",
                    "decision": verdict or "missing",
                    "reason": decision.get("reason", ""),
                }
            )
            if metrics is not None:
                metrics.record_approval_execution("rejected")
            logger.info(
                "[HITL] 不执行（决策=%s）tool=%s approval=%s", verdict, tool_name, approval_id
            )
            continue

        args = decision.get("edited_args") or action.get("arguments") or {}
        tool_call_id = f"approval:{approval_id}" if approval_id else ""

        if not registry.is_side_effect(tool_name):
            # 治理边界要求：被审批的高风险写操作必须声明 side_effect=True，
            # 否则 ledger 不会接管，等于失去崩溃重投的幂等保护。显式标注，
            # 不静默放行。
            logger.error(
                "[HITL] 高风险工具未声明 side_effect，缺少幂等保护 tool=%s approval=%s",
                tool_name,
                approval_id,
            )
            results.append(
                {
                    "tool": tool_name,
                    "approval_id": approval_id,
                    "status": "error",
                    "error": "ToolNotDeclaredSideEffect",
                }
            )
            if metrics is not None:
                metrics.record_approval_execution("error")
            continue

        try:
            result = await registry.execute_raw(tool_name, args, tool_call_id=tool_call_id)
            results.append(
                {
                    "tool": tool_name,
                    "approval_id": approval_id,
                    "status": "executed",
                    "decision": verdict,
                    "result": result,
                }
            )
            if metrics is not None:
                metrics.record_approval_execution("executed")
            logger.info("[HITL] 已批准并执行 tool=%s approval=%s", tool_name, approval_id)
        except Exception as e:
            # execute_raw 对 side_effect 工具的失败会向上冒泡（否则写失败会被
            # 伪装成成功），因此这里必须记录真实失败并让上层决定 retry/DLQ。
            results.append(
                {
                    "tool": tool_name,
                    "approval_id": approval_id,
                    "status": "error",
                    "error": type(e).__name__,
                    "detail": str(e)[:500],
                }
            )
            if metrics is not None:
                metrics.record_approval_execution("error")
            logger.error(
                "[HITL] 批准后执行失败 tool=%s approval=%s err=%s",
                tool_name,
                approval_id,
                type(e).__name__,
            )
    return results


__all__ = [
    "INTERRUPT_KEY",
    "collect_pending_actions",
    "execute_approved_actions",
    "has_pending_interrupt",
    "proposal_to_pending_action",
    "run_approval_gate",
    "should_propose_approval",
]
