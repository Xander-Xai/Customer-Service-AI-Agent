"""业务结果契约（Outcome Contract）—— **唯一真相源**。

它回答的不是"工作流跑完了没有"，而是两个此前被混为一谈的问题：

1. 图是否正常结束、是否真的把回复交付给了用户；
2. 这段回复是否**解决了**用户的业务问题，以及我们是否有证据。

背景缺陷
--------
``agents/response_agent.py::_evaluate_resolution`` 过去只看回复长度/关键词：
LLM 挂掉后由 ``RuleBasedLLM`` 或 ``fallback_response`` 生成的**通用兜底文案**
（例如"感谢您对我们产品的关注"）因为"够长、不含不确定词"被判成 ``resolved``，
随后被 ``core.monitoring`` 计入 ``total_single_turn_resolved``，并在 Agent Eval
里被算进 ``task_completion_rate``。结果就是"兜底文案 = 业务成功"。
本模块把"交付"与"解决"拆开，并显式区分"已解决"与"无法证明已解决"。

六个概念（全部为一等字段，不再复用单一个 ``resolved`` 布尔）
----------------------------------------------------------
======================  ===================================================
字段                    含义
======================  ===================================================
``workflow_finished``   工作流是否正常结束（无未捕获错误）。
``response_delivered``  是否向用户交付了非空回复。
``degraded``           是否发生 LLM / 工具 / 检索降级。
``business_resolved``  业务问题是否**实际被解决**；``None`` = 无法证明。
``outcome_verified``   是否有**独立证据**证明"解决"。
``requires_human_action`` 是否需要人工介入（HITL 挂起 / 明确转人工）。
======================  ===================================================

三级"完成"语义（本模块最重要的区分）
--------------------------------
一段非降级的正常回答**不等于**业务已解决。回答是 LLM 的自述，独立证据是别的东西。
因此把"完成"拆成三个等级，``business_outcome`` 取其中之一：

=========================  ==========================================  ==========
``business_outcome``       含义                                       ``outcome_verified``
=========================  ==========================================  ==========
``NOT_RESOLVED``           空回复 / 出错 / 截断 / 明确不确定            False
``REQUIRES_HUMAN``         HITL 待审批、被拒绝、明确转人工               False
``UNVERIFIED_DEGRADED``    交付了兜底模板，无任何成功证据                 False
``ASSESSED``              交付了实质回复且无降级，但**没有独立证据**     False
``EVIDENCED``              有独立证据（工具真的执行成功 / 审批放行后执行成功） True
=========================  ==========================================  ==========

对应地，``business_resolved`` 只在 ``EVIDENCED`` 时为 ``True``；``ASSESSED``
与 ``UNVERIFIED_DEGRADED`` 都是 ``None``（无法证明），其余为 ``False``。

**兼容性**：``ASSESSED`` 仍然映射成既有的 ``resolution_status == "resolved"``，
因此普通低风险问答的缓存写入、SLA 统计、看板口径**完全不变** —— 被收紧的只有
"业务已解决"这句话本身的含义。

设计纪律
--------
* **只用现有状态**：``response`` / ``resolution_status`` / ``pending_actions`` /
  ``approval_results`` / ``retrieval_degraded`` / ``error``。不新增第二套互相
  冲突的状态机。
* **降级 ≠ 失败，但降级 ≠ 解决**：这是本模块存在的最核心理由。一个良性的
  确定性工具结果可以成为完成证据；一段与问题无关的兜底模板则不能。
* **不确定时向"未验证"收敛**：拿不到证据就返回 ``business_resolved=None``
  或 ``False``，绝不因为"文本看起来像回答"而放行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── 解决状态词表（复用既有四值集合，不另立词汇）────────────────────────
RESOLUTION_RESOLVED = "resolved"
RESOLUTION_UNCERTAIN = "uncertain"
RESOLUTION_FAILED = "failed"
RESOLUTION_ESCALATED = "escalated"
RESOLUTION_STATUSES: tuple[str, ...] = (
    RESOLUTION_RESOLVED,
    RESOLUTION_UNCERTAIN,
    RESOLUTION_FAILED,
    RESOLUTION_ESCALATED,
)

#: 生产代码里**真实存在**的降级标记（响应文本或日志）。
#:
#: 这是 Agent Eval（``evaluation.agent_eval.contract``）与运行时
#: （``core.monitoring`` / ``agents.response_agent``）**共用的唯一一份清单**。
#: 新增一条降级路径而忘记登记 -> 漏检；登记了但生产里 grep 不到 -> 反向撒谎。
#: 契约由 ``tests/unit/test_agent_eval_contract.py`` 逐条回查生产源码守卫。
FALLBACK_MARKERS: tuple[tuple[str, str], ...] = (
    ("[circuit_breaker_open]", "熔断器打开，路由降级为纯规则"),
    ("[error_fallback]", "路由异常，降级为纯规则"),
    ("抱歉，处理问题时遇到错误", "BaseAgent 工具循环 / 通用兜底"),
    ("抱歉，处理售后问题时遇到错误", "售后 Agent 兜底"),
    ("抱歉，处理技术问题时遇到错误", "技术 Agent 兜底"),
    ("抱歉，处理产品查询时遇到问题", "产品 Agent 兜底"),
    ("抱歉，处理销售咨询时遇到问题", "销售 Agent 兜底"),
    ("抱歉，处理账单问题时遇到系统错误", "账单 Agent ERP 兜底"),
    ("抱歉，处理您的复杂问题时遇到困难", "ReActAgent 工具循环兜底"),
    ("抱歉，推理过程耗时过长", "ReAct 模式 SLA 超时降级"),
    ("抱歉，响应时间过长", "Sequential 模式 SLA 超时降级"),
    ("您的今日 Token 配额已用尽", "Token 配额耗尽降级"),
    ("无可用 Agent 响应", "编排层所有子 Agent 都失败"),
    ("Agent not found", "协作模式找不到请求的 Agent"),
    ("Primary agent", "Consultation 模式找不到主 Agent"),
    ("Coordinator", "Hierarchical 模式找不到协调者"),
    ("工具暂时不可用，请稍后重试", "工具执行异常兜底（结果未知，不得当作成功）"),
    # RuleBasedLLM 的 canned 回复（llm/rule_based_llm.py::RULE_TEMPLATES）。
    ("感谢您对我们产品的关注", "规则引擎降级：product_info 模板"),
    ("关于您的订单/账单问题", "规则引擎降级：billing 模板"),
    ("很高兴为您提供使用指导", "规则引擎降级：tech_support 模板"),
    ("RuleBasedLLM", "LLM 客户端降级到规则引擎（按类名记）"),
)

#: 只在**路由日志**里出现、不会出现在用户可见回复中的降级标记。用于把
#: "响应文本降级"与"路由层降级"分开，避免把路由降级误判成回复兜底。
LOG_ONLY_MARKERS: frozenset[str] = frozenset(
    {"[circuit_breaker_open]", "[error_fallback]", "RuleBasedLLM"}
)

#: 会被**直接交付给用户**的兜底/降级文案标记。``core`` 侧判"业务未解决"只认
#: 这一子集——它必须能从响应文本里匹配到。
RESPONSE_DEGRADATION_MARKERS: tuple[str, ...] = tuple(
    marker for marker, _meaning in FALLBACK_MARKERS if marker not in LOG_ONLY_MARKERS
)

#: BaseAgent 明确写进 state 的降级原因前缀（见 ``agents/base_agent.py``）。
_DEGRADED_STATE_KEYS = ("degraded_reason", "degradation_reason")


@dataclass(frozen=True)
class Outcome:
    """一次请求/一个 case 的业务结果。字段语义见模块 docstring。"""

    workflow_finished: bool
    response_delivered: bool
    degraded: bool
    business_resolved: bool | None
    outcome_verified: bool
    requires_human_action: bool
    business_outcome: str = ""
    evidence: tuple[str, ...] = ()
    degradation_flags: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_finished": self.workflow_finished,
            "response_delivered": self.response_delivered,
            "degraded": self.degraded,
            "business_resolved": self.business_resolved,
            "outcome_verified": self.outcome_verified,
            "requires_human_action": self.requires_human_action,
            "business_outcome": self.business_outcome,
            "evidence": list(self.evidence),
            "degradation_flags": list(self.degradation_flags),
            "reason": self.reason,
        }


#: ``business_outcome`` 的取值。与上面的文档表格一一对应，不要另立词汇。
BUSINESS_NOT_RESOLVED = "not_resolved"
BUSINESS_REQUIRES_HUMAN = "requires_human"
BUSINESS_UNVERIFIED_DEGRADED = "unverified_degraded"
BUSINESS_ASSESSED = "assessed"
BUSINESS_EVIDENCED = "evidenced"
BUSINESS_OUTCOMES: tuple[str, ...] = (
    BUSINESS_NOT_RESOLVED,
    BUSINESS_REQUIRES_HUMAN,
    BUSINESS_UNVERIFIED_DEGRADED,
    BUSINESS_ASSESSED,
    BUSINESS_EVIDENCED,
)


def detect_response_degradation(response: str) -> tuple[str, ...]:
    """从用户可见回复中检出兜底/降级模板，返回命中的标记（去重、稳定排序）。"""
    if not response:
        return ()
    hits = {marker for marker in RESPONSE_DEGRADATION_MARKERS if marker in response}
    return tuple(sorted(hits))


def _approval_requires_human(approval_results: list[dict[str, Any]]) -> bool:
    """审批结果里是否存在"仍需人工处理"的终态。

    ``executed`` 表示已获批准且执行完成，不再需要人工；``rejected`` /
    ``error`` / 缺失决策都表示业务动作没有完成，需要人工跟进。
    """
    for result in approval_results:
        status = str(result.get("status", "")).strip().lower()
        if status != "executed":
            return True
    return False


def _independent_evidence(state: dict[str, Any]) -> tuple[str, ...]:
    """收集"业务动作**确实发生**"的独立证据。

    只认两种，且都必须来自**执行侧**而不是 LLM 自述：

    1. ``state["tool_executions"]`` 里 ``ok=True`` 的记录 —— 由
       ``agents/base_agent.py::_record_tool_execution`` 在工具**真的返回**后写入；
       被 HITL 摘出、从未执行的调用不会出现在这里（它们只在 ``pending_actions``）。
    2. ``approval_results`` 里 ``status="executed"`` —— 人工批准后工具确实执行了。

    刻意**不**认的东西：非空的 ``response``、无降级标记、图正常结束。一段写得
    像模像样的回答只说明"系统在说话"，不说明"退款真的退了"。把它们当证据，就是
    把上一轮修掉的 bug（兜底文案算成功）换个说法再犯一次。
    """
    evidence: list[str] = []

    records = state.get("tool_executions") or []
    if isinstance(records, list):
        for record in records:
            if isinstance(record, dict) and record.get("ok"):
                evidence.append(f"tool_executed:{record.get('tool')}")

    results = state.get("approval_results") or []
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict):
                continue
            if str(result.get("status", "")).strip().lower() == "executed":
                evidence.append(f"approval_executed:{result.get('tool')}")

    return tuple(dict.fromkeys(evidence))


def classify_outcome(state: dict[str, Any]) -> Outcome:
    """从图状态推导业务结果。**纯函数**，不产生副作用、不写 state。"""
    response = str(state.get("response") or "")
    pending_actions = [
        p for p in (state.get("pending_actions") or []) if isinstance(p, dict) and p.get("tool")
    ]
    approval_results = [r for r in (state.get("approval_results") or []) if isinstance(r, dict)]

    workflow_finished = not bool(state.get("error"))
    response_delivered = bool(response.strip())

    flags: list[str] = list(detect_response_degradation(response))
    if state.get("retrieval_degraded"):
        flags.append("retrieval_degraded")
    for key in _DEGRADED_STATE_KEYS:
        if state.get(key):
            flags.append(str(state.get(key)))
            break
    degradation_flags = tuple(dict.fromkeys(flags))
    degraded = bool(degradation_flags)

    # ── 是否需要人工介入 ──────────────────────────────────────────────
    # 1) 仍有未执行的高风险动作挂在审批闸门上（WAITING_APPROVAL）；
    # 2) 审批被拒绝 / 执行报错 / 决策缺失；
    # 3) 回复明确要求转人工，且没有"已接管"的信号。
    requires_human = bool(pending_actions)
    if approval_results and _approval_requires_human(approval_results):
        requires_human = True
    escalated_phrase = any(
        phrase in response
        for phrase in ("转接人工", "转人工客服", "人工客服介入", "升级处理", "高级客服")
    )
    if escalated_phrase and not approval_results:
        requires_human = True

    evidence = _independent_evidence(state)

    # ── 分级：流程完成 / 业务可能解决 / 业务解决有证据 ────────────────
    if not workflow_finished or not response_delivered:
        business_outcome = BUSINESS_NOT_RESOLVED
    elif requires_human:
        # 业务动作尚未完成（等审批 / 被拒绝 / 需人工）。
        business_outcome = BUSINESS_REQUIRES_HUMAN
    elif degraded:
        # 交付的是兜底模板：连"可能解决"都算不上，只能标"未验证的降级"。
        business_outcome = BUSINESS_UNVERIFIED_DEGRADED
    elif evidence:
        # 有执行侧证据：某个业务动作真的跑了。
        business_outcome = BUSINESS_EVIDENCED
    else:
        # 有实质回复、无降级、但**没有**独立证据：只能说"可能解决"。
        business_outcome = BUSINESS_ASSESSED

    outcome_verified = business_outcome == BUSINESS_EVIDENCED
    if business_outcome == BUSINESS_EVIDENCED:
        business_resolved: bool | None = True
    elif business_outcome in (BUSINESS_ASSESSED, BUSINESS_UNVERIFIED_DEGRADED):
        business_resolved = None
    else:
        business_resolved = False

    reason = {
        BUSINESS_NOT_RESOLVED: (
            "workflow did not finish cleanly" if not workflow_finished else "no response delivered"
        ),
        BUSINESS_REQUIRES_HUMAN: "human action required (pending/rejected approval or escalation)",
        BUSINESS_UNVERIFIED_DEGRADED: "degraded fallback delivered; no verified task success",
        BUSINESS_ASSESSED: "substantive non-degraded answer delivered, but NO independent evidence",
        BUSINESS_EVIDENCED: "business action independently observed to have executed",
    }[business_outcome]

    return Outcome(
        workflow_finished=workflow_finished,
        response_delivered=response_delivered,
        degraded=degraded,
        business_resolved=business_resolved,
        outcome_verified=outcome_verified,
        requires_human_action=requires_human,
        business_outcome=business_outcome,
        evidence=evidence,
        degradation_flags=degradation_flags,
        reason=reason,
    )


def resolution_status_for(outcome: Outcome) -> str:
    """把 Outcome 映射回既有的四值 ``resolution_status`` 词表。

    兼容性要求：``ASSESSED``（实质回复、无降级、但无独立证据）仍然映射成
    ``resolved``。它是**系统自评的"这一轮正常作答了"**，不是"业务已验证解决" ——
    后者看 :attr:`Outcome.outcome_verified`。把两者混同会让缓存写入、SLA 统计
    与看板口径在收紧"已解决"时被一起改坏。
    """
    if outcome.requires_human_action:
        return RESOLUTION_ESCALATED
    if not outcome.workflow_finished or not outcome.response_delivered:
        return RESOLUTION_FAILED
    if outcome.business_outcome in (BUSINESS_ASSESSED, BUSINESS_EVIDENCED):
        return RESOLUTION_RESOLVED
    return RESOLUTION_UNCERTAIN


__all__ = [
    "BUSINESS_ASSESSED",
    "BUSINESS_EVIDENCED",
    "BUSINESS_NOT_RESOLVED",
    "BUSINESS_OUTCOMES",
    "BUSINESS_REQUIRES_HUMAN",
    "BUSINESS_UNVERIFIED_DEGRADED",
    "FALLBACK_MARKERS",
    "LOG_ONLY_MARKERS",
    "RESOLUTION_ESCALATED",
    "RESOLUTION_FAILED",
    "RESOLUTION_RESOLVED",
    "RESOLUTION_STATUSES",
    "RESOLUTION_UNCERTAIN",
    "RESPONSE_DEGRADATION_MARKERS",
    "Outcome",
    "classify_outcome",
    "detect_response_degradation",
    "resolution_status_for",
]
