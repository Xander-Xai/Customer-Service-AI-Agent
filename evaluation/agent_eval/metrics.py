"""Agent Eval V1 指标计算：显式分母、显式零样本、显式排除。

三条纪律
--------
1. **每个指标都带 ``numerator`` / ``denominator`` / ``excluded``**。
   只报一个百分数的评测无法复核，也无法区分"真的全对"和"分母只剩 1 条"。
2. **分母为 0 时不返回 0，也不返回 100** —— 返回 ``status="NOT_MEASURED"``。
   把"没样本"渲染成 0% 是评测系统最常见的撒谎方式：它看起来像"全军覆没"，
   实际上是"根本没跑"。
3. **不可达的 case 被显式排除并记账**，而不是当作 0 分。
   例如工具指标要求协作模式落在 ``react``（唯一持有 tool_registry 的模式）；
   落进 ``parallel`` 的 case 会让 tool selection 恒为 0，但那是数据集与编排
   现状不匹配，不是"系统不会选工具"。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from evaluation.evidence_schema import EvidenceSource

from .cases import AgentCase
from .contract import (
    DIAGNOSTIC_METRIC_NAMES,
    FALLBACK_MARKERS,
    METRIC_NAMES,
    TOOL_REACHABLE_MODE,
    WAITING_APPROVAL,
)
from .harness import CaseObservation

MEASURED = "MEASURED"
NOT_MEASURED = "NOT_MEASURED"
EXCLUDED = "EXCLUDED"


class Metric:
    """一个指标及其分母明细。"""

    def __init__(
        self,
        name: str,
        value: float | None,
        numerator: float,
        denominator: int,
        excluded: int,
        source: str = "APPLICATION_MEASURED",
        unit: str = "",
        notes: str = "",
        detail: dict[str, Any] | None = None,
    ):
        self.name = name
        self.value = value
        self.numerator = numerator
        self.denominator = denominator
        self.excluded = excluded
        self.source = EvidenceSource(source)
        self.unit = unit
        self.notes = notes
        self.detail = detail or {}

    @property
    def status(self) -> str:
        return MEASURED if self.value is not None else NOT_MEASURED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "excluded": self.excluded,
            "source": self.source.value
            if isinstance(self.source, EvidenceSource)
            else str(self.source),
            "unit": self.unit,
            "notes": self.notes,
            "detail": self.detail,
        }


def _measured(
    name: str, numerator: float, denominator: int, *, excluded: int = 0, **kwargs
) -> Metric:
    if denominator == 0:
        return _unmeasured(name, excluded=excluded, **kwargs)
    return Metric(name, numerator / denominator, numerator, denominator, excluded, **kwargs)


def _unmeasured(name: str, *, excluded: int = 0, **kwargs) -> Metric:
    return Metric(name, None, 0.0, 0, excluded, **kwargs)


def _ratio(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def _pair_cases(
    cases: Sequence[AgentCase], observations: Sequence[CaseObservation]
) -> list[tuple[AgentCase, CaseObservation]]:
    by_id = {o.case_id: o for o in observations}
    return [(c, by_id[c.case_id]) for c in cases if c.case_id in by_id]


def _tool_eligible(case: AgentCase, observation: CaseObservation) -> bool:
    """工具指标的分母资格。

    ``expect_fallback=True`` 的**故障注入** case 被显式排除：它按设计就让
    首轮 LLM 失败，于是工具调用**根本不应发生**。把它算进分母等于要求
    "注入故障后仍然要调工具"，那不是治理保真度，是把数据集的意图读反了。
    排除必须**记账**（计入 ``excluded``），不能悄悄消失。
    """
    if case.expect_fallback:
        return False
    return case.scores_tools() and observation.observed_mode == TOOL_REACHABLE_MODE


def _workflow_finished(observation: CaseObservation) -> bool:
    """工作流是否正常结束（无未捕获异常）。"""
    return observation.error is None


def _governance_outcome_matched(case: AgentCase, observation: CaseObservation) -> bool:
    """图是否抵达了**期望的终态**（含 ``WAITING_APPROVAL``）。

    这是"治理结果"而不是"业务结果"：高风险动作被正确摘出、图正确挂在
    ``__interrupt__`` 上、run 停在 ``WAITING_APPROVAL`` —— 这些都算**治理正确**，
    因为此时业务动作本来就**不该**被执行。
    """
    return observation.terminal_state == case.expected_terminal_state


def _business_completion_applicable(case: AgentCase) -> bool:
    """该 case 是否适用"业务任务完成"这一口径。

    ``WAITING_APPROVAL`` 的 case 按设计**没有**完成业务任务（它在等人审批），
    把它们算进业务完成率的分母等于用"正确地挂起"去惩罚"正确地完成"。因此它们
    被**排除**出业务完成口径，并计入 ``excluded`` 记账。
    """
    return case.expected_terminal_state != WAITING_APPROVAL


def _task_completed(case: AgentCase, observation: CaseObservation) -> bool:
    """业务任务是否**真的**完成 —— 而不是交付了一句兜底文案。

    判定顺序（任一不满足即为未完成）：

    1. 抵达 ``expected_terminal_state``；
    2. ``WAITING_APPROVAL`` **不算业务完成**（治理正确 ≠ 业务完成）；该口径下
       这些 case 根本不会被查询，见 :func:`_business_completion_applicable`；
    3. 必须有非空回复；
    4. 若观测到降级标记而 case 并未声明 ``expect_fallback``，判为未完成
       （这就是 ``fault_agent_llm_001`` / ``fault_tool_turn_001`` 此前被误
       统计为完成的原因）；
    5. case 显式声明 ``expect_task_completed=false`` 时不计入分子。
    """
    if not _governance_outcome_matched(case, observation):
        return False
    if not _business_completion_applicable(case):
        return False
    if not observation.response.strip():
        return False
    if observation.fallback_markers and not case.expect_fallback:
        return False
    return case.expect_task_completed


def _has_completion_evidence(case: AgentCase, observation: CaseObservation) -> bool:
    """完成结论是否有独立可核验证据。

    只有**真正执行过**的工具结果才算证据。刻意**不**把
    ``pending_actions`` / ``interrupt_payloads`` 当成证据：它们证明的是
    "闸门拦住了高风险动作"（治理证据），**不能**证明"退款真的退了、改单真的改了"
    （业务证据）。此前把 interrupt 当业务完成证据，等于用"成功拦截"冒充
    "成功执行"。
    """
    return bool(observation.executed_tool_names)


def compute_metrics(
    cases: Sequence[AgentCase], observations: Sequence[CaseObservation]
) -> dict[str, Metric]:
    pairs = _pair_cases(cases, observations)
    metrics: dict[str, Metric] = {}

    # ── route_accuracy（正式）──────────────────────────────────────────
    route_pairs = [(c, o) for c, o in pairs if c.scores_route()]
    shortcut = [(c, o) for c, o in route_pairs if o.route_source == "rule_shortcut"]
    if not shortcut:
        metrics["route_accuracy"] = _unmeasured(
            "route_accuracy",
            excluded=len(route_pairs),
            notes=(
                "no case landed on the deterministic rule_shortcut path; route accuracy has "
                "no referent without an LLM in the loop"
            ),
        )
    else:
        hits = sum(1 for c, o in shortcut if o.observed_route == c.expected_route)
        metrics["route_accuracy"] = _measured(
            "route_accuracy",
            hits,
            len(shortcut),
            excluded=len(route_pairs) - len(shortcut),
            unit="ratio",
            notes="denominator = cases whose route was decided by the deterministic rule path",
            detail={
                "disagreements": [
                    {
                        "case_id": c.case_id,
                        "expected": c.expected_route,
                        "observed": o.observed_route,
                    }
                    for c, o in shortcut
                    if o.observed_route != c.expected_route
                ]
            },
        )

    # ── rule_route_agreement（诊断）────────────────────────────────────
    labelled = [(c, o) for c, o in pairs if c.expected_route is not None]
    if not labelled:
        metrics["rule_route_agreement"] = _unmeasured(
            "rule_route_agreement", notes="no case carries an expected_route label"
        )
    else:
        hits = sum(1 for c, o in labelled if o.observed_route == c.expected_route)
        metrics["rule_route_agreement"] = _measured(
            "rule_route_agreement",
            hits,
            len(labelled),
            unit="ratio",
            notes="rule classifier only; this is NOT the end-to-end route result",
            detail={
                "disagreements": [
                    {
                        "case_id": c.case_id,
                        "expected": c.expected_route,
                        "observed": o.observed_route,
                    }
                    for c, o in labelled
                    if o.observed_route != c.expected_route
                ],
                "human_confirmed_only": sum(
                    1 for c, _o in labelled if c.annotation.is_human_confirmed
                ),
            },
        )

    # ── tool_selection_accuracy（正式）─────────────────────────────────
    eligible = [(c, o) for c, o in pairs if _tool_eligible(c, o)]
    unreachable = [
        c.case_id
        for c, o in pairs
        if c.scores_tools() and not _tool_eligible(c, o) and o.observed_mode == TOOL_REACHABLE_MODE
    ]
    fault_excluded = [c.case_id for c, _o in pairs if c.scores_tools() and c.expect_fallback]
    if not eligible:
        metrics["tool_selection_accuracy"] = _unmeasured(
            "tool_selection_accuracy",
            excluded=len(unreachable) + len(fault_excluded),
            notes=(
                f"no case with expected_tools reached the tool-capable collaboration mode ({TOOL_REACHABLE_MODE})"
            ),
            detail={
                "unreachable_cases": unreachable,
                "fault_injection_excluded": fault_excluded,
            },
        )
    else:
        hits = 0
        misses: list[dict[str, Any]] = []
        for case, obs in eligible:
            expected = set(case.expected_tools)
            requested = set(obs.requested_tool_names)
            # 治理层保真度：被 HITL 摘下的调用**仍然算作"按计划发起了"**。
            if requested == expected:
                hits += 1
            else:
                misses.append(
                    {
                        "case_id": case.case_id,
                        "expected": sorted(expected),
                        "requested": sorted(requested),
                        "deferred": sorted(obs.deferred_tool_names),
                    }
                )
        metrics["tool_selection_accuracy"] = _measured(
            "tool_selection_accuracy",
            hits,
            len(eligible),
            excluded=len(unreachable) + len(fault_excluded),
            unit="ratio",
            notes=(
                "governance-layer fidelity for a scripted tool plan; "
                "not model tool-choice quality. fault-injection cases are excluded "
                "because their tool calls are designed NOT to happen."
            ),
            detail={
                "misses": misses,
                "unreachable_cases": unreachable,
                "fault_injection_excluded": fault_excluded,
            },
        )

    # ── tool_argument_schema_pass_rate（正式）──────────────────────────
    observed_calls = [c for c, o in pairs for c in o.tool_calls]
    if not observed_calls:
        metrics["tool_argument_schema_pass_rate"] = _unmeasured(
            "tool_argument_schema_pass_rate",
            notes="no tool call was observed in the eligible population",
        )
    else:
        ok = sum(1 for call in observed_calls if not call.schema_errors)
        metrics["tool_argument_schema_pass_rate"] = _measured(
            "tool_argument_schema_pass_rate",
            ok,
            len(observed_calls),
            unit="ratio",
            notes="denominator = observed tool calls (one argument bundle each)",
            detail={
                "failures": [
                    {"name": c.name, "errors": list(c.schema_errors)}
                    for c in observed_calls
                    if c.schema_errors
                ]
            },
        )

    # ── expected_parameter_match_rate（诊断）────────────────────────────
    # 参数**语义期望**校验：observed 调用是否含有数据集声明的期望参数（子集匹配）。
    # 与 tool_argument_schema_pass_rate（schema 校验）互补，且明确是诊断口径 ——
    # 参数由脚本化 LLM 给出，因此它衡量"期望参数是否端到端被保留"，不是模型的参数推理。
    param_pairs = [(c, o) for c, o in pairs if c.expected_parameters]
    param_excluded = [c.case_id for c, _o in param_pairs if c.expect_fallback]
    param_eligible = [(c, o) for c, o in param_pairs if not c.expect_fallback]
    param_slots = sum(len(c.expected_parameters) for c, _o in param_eligible)
    if param_slots == 0:
        metrics["expected_parameter_match_rate"] = _unmeasured(
            "expected_parameter_match_rate",
            excluded=len(param_excluded),
            notes="no non-fault case declares expected_parameters",
        )
    else:
        matched = 0
        param_misses: list[dict[str, Any]] = []
        for case, obs in param_eligible:
            for tool_name, expected_args in case.expected_parameters.items():
                calls = [call for call in obs.tool_calls if call.name == tool_name]
                if any(
                    all(call.arguments.get(key) == value for key, value in expected_args.items())
                    for call in calls
                ):
                    matched += 1
                else:
                    param_misses.append(
                        {
                            "case_id": case.case_id,
                            "tool": tool_name,
                            "expected": dict(expected_args),
                            "observed": [dict(call.arguments) for call in calls],
                        }
                    )
        metrics["expected_parameter_match_rate"] = _measured(
            "expected_parameter_match_rate",
            matched,
            param_slots,
            excluded=len(param_excluded),
            unit="ratio",
            notes=(
                "denominator = (case, tool) slots declared in expected_parameters across "
                "non-fault cases; numerator = slots where an observed call for that tool "
                "carried every expected key/value (subset match). Diagnostic: parameters come "
                "from the scripted LLM, so this checks end-to-end preservation, not model reasoning."
            ),
            detail={"misses": param_misses},
        )

    # ── forbidden_tool_rate（正式）─────────────────────────────────────
    forbidden_cases = [(c, o) for c, o in pairs if c.forbidden_tools]
    all_calls = [call for _c, o in pairs for call in o.tool_calls]
    if not forbidden_cases:
        metrics["forbidden_tool_rate"] = _unmeasured(
            "forbidden_tool_rate",
            notes="no case declares forbidden_tools",
            detail={"observed_calls": len(all_calls)},
        )
    else:
        hits = sum(
            1
            for case, obs in forbidden_cases
            for call in obs.tool_calls
            if call.name in case.forbidden_tools
        )
        metrics["forbidden_tool_rate"] = _measured(
            "forbidden_tool_rate",
            hits,
            len(forbidden_cases),
            unit="ratio",
            notes="hits on forbidden_tools among cases that declare them (target 0)",
            detail={"forbidden_hits": hits},
        )

    # ── hitl_trigger_accuracy（正式）───────────────────────────────────
    risk_pairs = [(c, o) for c, o in pairs if c.scores_risk() and o.tool_calls]
    if not risk_pairs:
        metrics["hitl_trigger_accuracy"] = _unmeasured(
            "hitl_trigger_accuracy",
            notes="no case with expected_risk produced an observable tool call",
        )
    else:
        hits = 0
        failures: list[dict[str, Any]] = []
        for case, obs in risk_pairs:
            ok = True
            for call in obs.tool_calls:
                if call.policy_requires_approval:
                    ok = ok and call.deferred and not call.executed
                else:
                    ok = ok and not call.deferred
            if ok:
                hits += 1
            else:
                failures.append(
                    {
                        "case_id": case.case_id,
                        "expected_risk": case.expected_risk,
                        "calls": [c.to_dict() for c in obs.tool_calls],
                    }
                )
        metrics["hitl_trigger_accuracy"] = _measured(
            "hitl_trigger_accuracy",
            hits,
            len(risk_pairs),
            unit="ratio",
            notes=(
                "high-risk case: every HIGH call must be deferred and none executed; "
                "read-only calls may (and should) still run. "
                "low/medium-risk case: nothing may be gated, and every observed call must "
                "actually execute. denominator = cases with expected_risk that produced at "
                "least one observable tool call."
            ),
            detail={"failures": failures},
        )

    # ── hitl_gate_propagation（诊断）───────────────────────────────────
    gate_pairs = [
        (c, o)
        for c, o in pairs
        if o.deferred_tool_names and c.expected_terminal_state == WAITING_APPROVAL
    ]
    if not gate_pairs:
        metrics["hitl_gate_propagation"] = _unmeasured(
            "hitl_gate_propagation",
            notes="no case both deferred a HIGH-risk action and expects WAITING_APPROVAL",
        )
    else:
        hits = sum(
            1
            for _c, o in gate_pairs
            if o.pending_actions and o.interrupt_payloads and o.terminal_state == WAITING_APPROVAL
        )
        metrics["hitl_gate_propagation"] = _measured(
            "hitl_gate_propagation",
            hits,
            len(gate_pairs),
            unit="ratio",
            notes=(
                "diagnostic: end-to-end connectivity from tool-loop deferral to the LangGraph "
                "interrupt. Independent of the durable resume path (see make runtime-e2e)."
            ),
            detail={
                "failures": [
                    {
                        "case_id": c.case_id,
                        "deferred": list(o.deferred_tool_names),
                        "pending_actions": len(o.pending_actions),
                        "interrupts": len(o.interrupt_payloads),
                        "terminal_state": o.terminal_state,
                    }
                    for c, o in gate_pairs
                    if not (o.pending_actions and o.interrupt_payloads)
                ]
            },
        )

    # ── governance_outcome_match_rate（正式）──────────────────────────
    # 「图是否抵达了期望终态」——含 WAITING_APPROVAL。治理正确 ≠ 业务完成。
    if not pairs:
        metrics["governance_outcome_match_rate"] = _unmeasured(
            "governance_outcome_match_rate", notes="no case observed"
        )
    else:
        gov_matched = [(c, o) for c, o in pairs if _governance_outcome_matched(c, o)]
        metrics["governance_outcome_match_rate"] = _measured(
            "governance_outcome_match_rate",
            len(gov_matched),
            len(pairs),
            unit="ratio",
            notes=(
                "denominator = all observed cases; numerator = cases whose terminal state "
                "equals expected_terminal_state. WAITING_APPROVAL counts here (the graph "
                "correctly stopped and waited for a human) but NOT in task_completion_rate."
            ),
            detail={
                "misses": [
                    {
                        "case_id": c.case_id,
                        "expected_state": c.expected_terminal_state,
                        "observed_state": o.terminal_state,
                    }
                    for c, o in pairs
                    if not _governance_outcome_matched(c, o)
                ]
            },
        )

    # ── task_completion_rate（正式）────────────────────────────────────
    business_pairs = [(c, o) for c, o in pairs if _business_completion_applicable(c)]
    not_applicable = len(pairs) - len(business_pairs)
    if not business_pairs:
        metrics["task_completion_rate"] = _unmeasured(
            "task_completion_rate",
            excluded=not_applicable,
            notes="no case is applicable to the business-completion population",
        )
    else:
        completed = [(c, o) for c, o in business_pairs if _task_completed(c, o)]
        metrics["task_completion_rate"] = _measured(
            "task_completion_rate",
            len(completed),
            len(business_pairs),
            excluded=not_applicable,
            unit="ratio",
            notes=(
                "denominator = cases where business completion is APPLICABLE; WAITING_APPROVAL "
                "cases are excluded (and counted in `excluded`) because they have, by design, "
                "not completed a business task — counting them as completions would let "
                "'correctly paused for approval' masquerade as 'refund executed'. "
                "fault-injection cases stay in the denominator and honestly lower the rate."
            ),
            detail={
                "not_applicable_to_business_completion": [
                    c.case_id for c, o in pairs if not _business_completion_applicable(c)
                ],
                "misses": [
                    {
                        "case_id": c.case_id,
                        "expected_state": c.expected_terminal_state,
                        "observed_state": o.terminal_state,
                        "degraded": bool(o.fallback_markers),
                        "expect_task_completed": c.expect_task_completed,
                        "error": o.error,
                    }
                    for c, o in business_pairs
                    if not _task_completed(c, o)
                ],
            },
        )

    # ── workflow_execution_rate（正式）─────────────────────────────────
    if not pairs:
        metrics["workflow_execution_rate"] = _unmeasured(
            "workflow_execution_rate", notes="no case observed"
        )
    else:
        finished = [(c, o) for c, o in pairs if _workflow_finished(o)]
        metrics["workflow_execution_rate"] = _measured(
            "workflow_execution_rate",
            len(finished),
            len(pairs),
            unit="ratio",
            notes="denominator = all observed cases; numerator = graph ended without an "
            "unhandled exception",
            detail={
                "unfinished": [
                    {"case_id": c.case_id, "error": o.error}
                    for c, o in pairs
                    if not _workflow_finished(o)
                ]
            },
        )

    # ── response_delivery_rate（正式）──────────────────────────────────
    if not pairs:
        metrics["response_delivery_rate"] = _unmeasured(
            "response_delivery_rate", notes="no case observed"
        )
    else:
        delivered = [(c, o) for c, o in pairs if o.response.strip()]
        metrics["response_delivery_rate"] = _measured(
            "response_delivery_rate",
            len(delivered),
            len(pairs),
            unit="ratio",
            notes=(
                "denominator = all observed cases; numerator = non-empty delivered response. "
                "WAITING_APPROVAL cases legitimately deliver nothing (the graph pauses before "
                "the final response) — delivery is not completion."
            ),
            detail={"undelivered": [c.case_id for c, o in pairs if not o.response.strip()]},
        )

    # ── task_completion_evidence_coverage（正式）───────────────────────
    completed_pairs = [(c, o) for c, o in pairs if _task_completed(c, o)]
    if not completed_pairs:
        metrics["task_completion_evidence_coverage"] = _unmeasured(
            "task_completion_evidence_coverage",
            notes="no case was counted as completed; evidence coverage has no denominator",
        )
    else:
        evidenced = [(c, o) for c, o in completed_pairs if _has_completion_evidence(c, o)]
        metrics["task_completion_evidence_coverage"] = _measured(
            "task_completion_evidence_coverage",
            len(evidenced),
            len(completed_pairs),
            unit="ratio",
            notes=(
                "denominator = cases counted as completed by task_completion_rate; numerator = "
                "completed cases backed by independently checkable evidence — i.e. a tool that "
                "actually executed and returned. An interrupt / pending_action is NOT business "
                "evidence: it proves the gate blocked a HIGH-risk action, not that the action "
                "happened. A scripted LLM's prose answer is likewise not verifiable."
            ),
            detail={
                "unevidenced_completions": [
                    {"case_id": c.case_id, "terminal_state": o.terminal_state}
                    for c, o in completed_pairs
                    if not _has_completion_evidence(c, o)
                ]
            },
        )

    # ── fallback_rate（正式）───────────────────────────────────────────
    if not pairs:
        metrics["fallback_rate"] = _unmeasured("fallback_rate", notes="no case observed")
    else:
        hits = sum(1 for _c, o in pairs if o.fallback_markers)
        metrics["fallback_rate"] = _measured(
            "fallback_rate",
            hits,
            len(pairs),
            unit="ratio",
            notes=(
                "denominator = all observed cases; a case counts once even if several "
                "degradation markers fired"
            ),
            detail={
                "marker_hits": {
                    marker: sum(1 for _c, o in pairs if marker in o.fallback_markers)
                    for marker, _meaning in FALLBACK_MARKERS
                }
            },
        )

    # ── fallback_detection_accuracy（正式）─────────────────────────────
    declared_or_fired = [(c, o) for c, o in pairs if c.expect_fallback or o.fallback_markers]
    if not declared_or_fired:
        metrics["fallback_detection_accuracy"] = _unmeasured(
            "fallback_detection_accuracy",
            notes="no case declares expect_fallback and none triggered a degradation marker",
        )
    else:
        hits = sum(1 for c, o in declared_or_fired if c.expect_fallback == bool(o.fallback_markers))
        metrics["fallback_detection_accuracy"] = _measured(
            "fallback_detection_accuracy",
            hits,
            len(declared_or_fired),
            unit="ratio",
            notes="denominator = cases where expect_fallback is true OR a marker fired",
            detail={
                "mismatches": [
                    {
                        "case_id": c.case_id,
                        "expect_fallback": c.expect_fallback,
                        "markers": list(o.fallback_markers),
                    }
                    for c, o in declared_or_fired
                    if c.expect_fallback != bool(o.fallback_markers)
                ]
            },
        )

    # ── step_count（诊断）──────────────────────────────────────────────
    if not pairs:
        metrics["step_count"] = Metric(
            "step_count", None, 0.0, 0, 0, unit="node_executions", notes="no case observed"
        )
    else:
        counts = [o.step_count for _c, o in pairs]
        metrics["step_count"] = Metric(
            "step_count",
            float(sum(counts)),
            float(sum(counts)),
            len(pairs),
            0,
            unit="node_executions",
            notes=(
                "value is the SUM of node executions over all cases; read detail.mean_steps / "
                "detail.max_steps_observed for the distribution and detail.within_budget for the gate"
            ),
            detail={
                "mean_steps": round(sum(counts) / len(counts), 3),
                "max_steps_observed": max(counts),
                "within_budget": sum(1 for c, o in pairs if o.step_count <= c.max_steps),
                "over_budget": [
                    {"case_id": c.case_id, "budget": c.max_steps, "observed": o.step_count}
                    for c, o in pairs
                    if o.step_count > c.max_steps
                ],
            },
        )

    missing = set(METRIC_NAMES + DIAGNOSTIC_METRIC_NAMES) - set(metrics)
    if missing:  # pragma: no cover - 契约与实现不同步
        raise AssertionError(f"compute_metrics did not produce: {sorted(missing)}")
    return metrics


__all__ = ["EXCLUDED", "MEASURED", "NOT_MEASURED", "Metric", "compute_metrics"]
