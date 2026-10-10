"""P0 业务完成率负控：交付 ≠ 解决，降级兜底不得算成功。

这些测试存在的理由：历史实现把"够长、不含不确定词"的**兜底文案**判成
``resolved``，于是 ``total_single_turn_resolved`` 与 Agent Eval 的
``task_completion_rate`` 把 LLM 故障 / 工具失败后的兜底当成业务成功。
本文件用负控把这条路径钉死，并验证运行时监控与评测侧采用同一套定义。
"""

from __future__ import annotations

import pytest

from core.outcome import (
    RESOLUTION_ESCALATED,
    RESOLUTION_RESOLVED,
    RESOLUTION_UNCERTAIN,
    classify_outcome,
    detect_response_degradation,
    resolution_status_for,
)

pytestmark = pytest.mark.unit


class TestOutcomeContract:
    def test_plain_answer_is_assessed_not_evidently_solved(self):
        """正常非降级回答只是「可能解决」，**不是**「已验证解决」。

        这是本轮语义收紧的核心：回答本身是 LLM 的自述，不构成业务成功的
        独立证据。但它仍必须映射成 ``resolution_status == "resolved"``，
        否则普通问答的缓存写入 / SLA 统计会被一起改坏。
        """
        outcome = classify_outcome(
            {"response": "订单 RO-1 已于昨天发出，预计明天送达，请留意物流。"}
        )
        assert outcome.workflow_finished is True
        assert outcome.response_delivered is True
        assert outcome.degraded is False
        assert outcome.business_outcome == "assessed"
        assert outcome.business_resolved is None
        assert outcome.outcome_verified is False
        assert outcome.requires_human_action is False
        assert resolution_status_for(outcome) == RESOLUTION_RESOLVED

    def test_executed_tool_is_independent_evidence(self):
        """工具**真的执行成功**才构成业务解决的独立证据。"""
        state = {
            "response": "已为您办理退款，金额 99 元。",
            "tool_executions": [{"tool": "refund", "ok": True}],
        }
        outcome = classify_outcome(state)
        assert outcome.business_outcome == "evidenced"
        assert outcome.business_resolved is True
        assert outcome.outcome_verified is True
        assert "tool_executed:refund" in outcome.evidence

    def test_failed_tool_execution_is_not_evidence(self):
        state = {
            "response": "抱歉，退款未能办理，请稍后重试。",
            "tool_executions": [{"tool": "refund", "ok": False}],
        }
        outcome = classify_outcome(state)
        assert outcome.outcome_verified is False
        assert outcome.business_outcome == "assessed"
        assert outcome.evidence == ()

    def test_deferred_hitl_action_is_never_evidence(self):
        """被摘出待审批的工具**从未执行**，不能算已执行的证据。"""
        state = {
            "response": "该操作需要人工审批，已提交审批请求。",
            "pending_actions": [{"tool": "refund", "arguments": {}}],
            "tool_executions": [],
        }
        outcome = classify_outcome(state)
        assert outcome.outcome_verified is False
        assert outcome.business_outcome == "requires_human"
        assert outcome.evidence == ()

    def test_rule_based_llm_template_is_degraded_not_resolved(self):
        # RuleBasedLLM 的 canned 模板：够长、不含不确定词，但**与用户问题无关**。
        state = {"response": "您好！感谢您对我们产品的关注。\n\n我们的产品线丰富，欢迎选购。"}
        outcome = classify_outcome(state)
        assert outcome.degraded is True
        assert outcome.outcome_verified is False
        assert outcome.business_resolved is None  # UNKNOWN，绝不是 True
        assert resolution_status_for(outcome) == RESOLUTION_UNCERTAIN

    def test_generic_fallback_is_not_resolved(self):
        state = {"response": "抱歉，处理问题时遇到错误，请稍后重试。"}
        outcome = classify_outcome(state)
        assert outcome.degraded is True
        assert outcome.business_resolved is None
        assert resolution_status_for(outcome) == RESOLUTION_UNCERTAIN

    def test_tool_failure_unknown_result_is_not_success(self):
        state = {"response": "抱歉，处理账单问题时遇到系统错误，请稍后再试。"}
        outcome = classify_outcome(state)
        assert outcome.degraded is True
        assert outcome.business_resolved is not True
        assert resolution_status_for(outcome) != RESOLUTION_RESOLVED

    def test_empty_response_failed(self):
        outcome = classify_outcome({"response": ""})
        assert outcome.response_delivered is False
        assert outcome.business_resolved is False
        assert resolution_status_for(outcome) == "failed"

    def test_hitl_pending_requires_human_not_resolved(self):
        state = {
            "response": "该操作需要人工审批，已提交审批请求。",
            "pending_actions": [{"tool": "staging_refund", "arguments": {"order_id": "O-1"}}],
        }
        outcome = classify_outcome(state)
        assert outcome.requires_human_action is True
        assert outcome.business_resolved is False
        assert outcome.outcome_verified is False
        assert resolution_status_for(outcome) == RESOLUTION_ESCALATED

    def test_hitl_rejected_is_not_resolved(self):
        state = {
            "response": "审批未通过，退款未执行。",
            "approval_results": [
                {"tool": "staging_refund", "status": "rejected", "decision": "reject"}
            ],
        }
        outcome = classify_outcome(state)
        assert outcome.requires_human_action is True
        assert outcome.business_resolved is False

    def test_hitl_approved_and_executed_is_evidence(self):
        state = {
            "response": "退款已按您的审批结果执行完成。",
            "approval_results": [{"tool": "staging_refund", "status": "executed", "result": "ok"}],
        }
        outcome = classify_outcome(state)
        assert outcome.requires_human_action is False
        assert outcome.business_outcome == "evidenced"
        assert outcome.business_resolved is True
        assert outcome.outcome_verified is True
        assert "approval_executed:staging_refund" in outcome.evidence

    def test_partial_success_overall_not_resolved(self):
        # 一个动作成功、另一个被拒 -> 整体业务未完成。
        state = {
            "response": "已处理部分请求。",
            "approval_results": [
                {"tool": "staging_refund", "status": "executed"},
                {"tool": "staging_order_change", "status": "rejected"},
            ],
        }
        outcome = classify_outcome(state)
        assert outcome.requires_human_action is True
        assert outcome.business_outcome == "requires_human"
        assert outcome.business_resolved is False
        assert outcome.outcome_verified is False

    def test_explicit_handoff_is_requires_human(self):
        state = {"response": "您的问题需要更高级别的处理，正在为您转接人工客服。"}
        outcome = classify_outcome(state)
        assert outcome.requires_human_action is True
        assert outcome.business_resolved is False

    def test_duplicate_task_historical_valid_result_is_assessed(self):
        # 缓存命中返回历史有效结果：交付有效、非降级 —— 但同样**没有**独立证据，
        # 因此是 ASSESSED（可能解决），不是 EVIDENCED（已验证解决）。
        state = {"response": "订单 RO-1 已于昨天发出，预计明天送达。", "cached": True}
        outcome = classify_outcome(state)
        assert outcome.business_outcome == "assessed"
        assert outcome.business_resolved is None
        assert outcome.outcome_verified is False
        assert resolution_status_for(outcome) == RESOLUTION_RESOLVED

    def test_retrieval_degraded_flag_marks_degraded(self):
        state = {"response": "为您找到以下信息：xxx", "retrieval_degraded": True}
        outcome = classify_outcome(state)
        assert outcome.degraded is True
        assert outcome.business_resolved is None

    def test_detect_response_degradation_matches_only_user_visible_markers(self):
        assert detect_response_degradation("正常回答，没有问题") == ()
        assert "感谢您对我们产品的关注" in detect_response_degradation(
            "您好！感谢您对我们产品的关注。"
        )


class TestResponseAgentUsesContract:
    def test_fallback_template_not_cached_as_resolved(self):
        from agents.response_agent import RESOLUTION_UNCERTAIN, ResponseAgent

        agent = ResponseAgent()
        state = {"response": "您好！感谢您对我们产品的关注。产品线丰富，欢迎选购。"}
        assert agent._evaluate_resolution(state) == RESOLUTION_UNCERTAIN
        assert state["degraded"] is True

    def test_hitl_pending_is_escalated(self):
        from agents.response_agent import RESOLUTION_ESCALATED, ResponseAgent

        agent = ResponseAgent()
        state = {
            "response": "该操作需要人工审批，已提交审批请求。",
            "pending_actions": [{"tool": "staging_refund", "arguments": {}}],
        }
        assert agent._evaluate_resolution(state) == RESOLUTION_ESCALATED

    def test_plain_answer_stays_resolved_for_legacy_callers(self):
        """兼容性回归：普通回答仍是 ``resolved``（缓存/SLA 口径不变）。

        ``resolved`` 表示"这一轮正常作答了"，不是"业务已验证解决"。若这里改成
        uncertain，普通问答的缓存写入与 ``total_single_turn_resolved`` 会一起失效。
        """
        from agents.response_agent import RESOLUTION_RESOLVED, ResponseAgent

        agent = ResponseAgent()
        state = {"response": "这款精华含有透明质酸成分，适合干性肤质。"}
        assert agent._evaluate_resolution(state) == RESOLUTION_RESOLVED
        # ...但它**没有**独立证据，因此不算"已验证解决"
        assert state["outcome"].outcome_verified is False
        assert state["outcome"].business_outcome == "assessed"


class TestMonitoringDoesNotCountDegradedAsResolved:
    @pytest.mark.asyncio
    async def test_degraded_never_counts_as_single_turn_resolved(self):
        from core.monitoring import MetricsCollector

        metrics = MetricsCollector()
        # 即使上层错把兜底标成 resolved，degraded=True 也必须拦住。
        await metrics.record_request(
            elapsed=1.0,
            session_id="s-degraded",
            resolution_status="resolved",
            degraded=True,
        )
        assert metrics.total_single_turn_resolved == 0
        assert metrics.total_degraded == 1

    @pytest.mark.asyncio
    async def test_genuine_resolution_still_counts(self):
        from core.monitoring import MetricsCollector

        metrics = MetricsCollector()
        await metrics.record_request(
            elapsed=1.0, session_id="s-ok", resolution_status="resolved", degraded=False
        )
        assert metrics.total_single_turn_resolved == 1
        assert metrics.total_degraded == 0


class TestEvalTaskCompletionNegativeControl:
    def _obs(self, **overrides):
        from evaluation.agent_eval.harness import CaseObservation

        base = dict(
            case_id="c",
            nodes_executed=("a",),
            step_count=1,
            observed_route=None,
            route_source="unavailable",
            rule_route=None,
            rule_confidence=0.0,
            router_llm_calls=0,
            observed_mode="sequential",
            resolution_status=None,
            response="回答",
            terminal_state="SUCCEEDED",
            tool_calls=(),
            fallback_markers=(),
            route_shortcut_used=False,
            hitl_defer_logged=False,
            pending_actions=(),
            interrupt_payloads=(),
            side_effect_counters={},
            scripted_plan_remaining=0,
            scripted_tool_rounds=0,
            elapsed_ms=1.0,
        )
        base.update(overrides)
        return CaseObservation(**base)

    def test_fault_case_is_not_counted_as_completed(self):
        from evaluation.agent_eval.cases import AgentCase
        from evaluation.agent_eval.metrics import compute_metrics

        case = AgentCase(
            case_id="fault",
            input="q",
            expected_terminal_state="SUCCEEDED",
            max_steps=5,
            expect_fallback=True,
            expect_task_completed=False,
        )
        obs = self._obs(case_id="fault", fallback_markers=("感谢您对我们产品的关注",))
        metrics = compute_metrics([case], [obs])
        assert metrics["task_completion_rate"].value == 0.0
        assert metrics["task_completion_rate"].denominator == 1
        assert metrics["fallback_rate"].value == 1.0

    def test_unexpected_degradation_is_not_counted_as_completed(self):
        from evaluation.agent_eval.cases import AgentCase
        from evaluation.agent_eval.metrics import compute_metrics

        # 非故障 case 却发生了降级 -> 不得算完成（这正是历史假阳性的形态）。
        case = AgentCase(
            case_id="normal", input="q", expected_terminal_state="SUCCEEDED", max_steps=5
        )
        obs = self._obs(case_id="normal", fallback_markers=("抱歉，处理问题时遇到错误",))
        metrics = compute_metrics([case], [obs])
        assert metrics["task_completion_rate"].value == 0.0

    def test_clean_completion_without_tool_evidence_has_low_coverage(self):
        from evaluation.agent_eval.cases import AgentCase
        from evaluation.agent_eval.metrics import compute_metrics

        case = AgentCase(
            case_id="normal", input="q", expected_terminal_state="SUCCEEDED", max_steps=5
        )
        obs = self._obs(case_id="normal")
        metrics = compute_metrics([case], [obs])
        assert metrics["task_completion_rate"].value == 1.0
        assert metrics["task_completion_evidence_coverage"].value == 0.0
        assert metrics["task_completion_evidence_coverage"].denominator == 1


def _hitl_case(**overrides):
    from evaluation.agent_eval.cases import AgentCase

    base = dict(
        case_id="hitl",
        input="帮我退款",
        expected_terminal_state="WAITING_APPROVAL",
        max_steps=5,
        expected_risk="high",
        expect_task_completed=False,
    )
    base.update(overrides)
    return AgentCase(**base)


class TestWaitingApprovalIsGovernanceNotBusinessCompletion:
    """WAITING_APPROVAL 计入治理结果，**绝不**计入业务完成。"""

    def _waiting_obs(self, **overrides):
        from evaluation.agent_eval.harness import CaseObservation

        base = dict(
            case_id="hitl",
            nodes_executed=("react", "human_approval_gate"),
            step_count=2,
            observed_route="billing",
            route_source="rule_shortcut",
            rule_route="billing",
            rule_confidence=0.9,
            router_llm_calls=0,
            observed_mode="react",
            resolution_status="escalated",
            response="该操作需要人工审批，已提交审批请求。",
            terminal_state="WAITING_APPROVAL",
            tool_calls=(),
            fallback_markers=(),
            route_shortcut_used=True,
            hitl_defer_logged=True,
            pending_actions=({"tool": "staging_refund", "arguments": {}},),
            interrupt_payloads=({"approval_id": "AP-1"},),
            side_effect_counters={},
            scripted_plan_remaining=0,
            scripted_tool_rounds=0,
            elapsed_ms=1.0,
        )
        base.update(overrides)
        return CaseObservation(**base)

    def test_waiting_approval_counts_as_governance_outcome_match(self):
        from evaluation.agent_eval.metrics import compute_metrics

        metrics = compute_metrics([_hitl_case()], [self._waiting_obs()])
        gov = metrics["governance_outcome_match_rate"]
        assert gov.value == 1.0
        assert gov.denominator == 1

    def test_waiting_approval_is_excluded_from_business_completion(self):
        from evaluation.agent_eval.metrics import compute_metrics

        metrics = compute_metrics([_hitl_case()], [self._waiting_obs()])
        tcr = metrics["task_completion_rate"]
        # 业务完成口径下它**不适用**：既不在分子，也不在分母，而是被记账为 excluded。
        assert tcr.status == "NOT_MEASURED"
        assert tcr.numerator == 0
        assert tcr.denominator == 0
        assert tcr.excluded == 1

    def test_interrupt_is_governance_evidence_not_business_evidence(self):
        """pending_actions + interrupt 证明的是「拦住了」，不是「退成功了」。"""
        from evaluation.agent_eval.metrics import _has_completion_evidence

        obs = self._waiting_obs()
        assert _has_completion_evidence(_hitl_case(), obs) is False

    def test_approval_rejected_is_not_completed(self):
        """审批被拒后 run 回到 SUCCEEDED，但业务动作没做 -> 不是业务完成。

        运行时语义由 :func:`core.outcome.classify_outcome` 判定（见
        ``test_hitl_rejected_is_not_resolved``）。这里锁住评测侧的等价事实：
        终态相符**不足以**构成业务完成。
        """
        from evaluation.agent_eval.metrics import compute_metrics

        case = _hitl_case(expected_terminal_state="SUCCEEDED", expect_task_completed=True)
        obs = self._waiting_obs(
            case_id="hitl",
            terminal_state="SUCCEEDED",
            pending_actions=(),
            interrupt_payloads=(),
            response="很抱歉，退款申请未获审批，未执行。",
        )
        metrics = compute_metrics([case], [obs])
        # 治理口径：终态相符 -> 治理正确
        assert metrics["governance_outcome_match_rate"].value == 1.0
        # 业务完成口径：case 自声明 expect_task_completed=true，但没有任何执行证据
        assert metrics["task_completion_rate"].value == 1.0
        assert metrics["task_completion_evidence_coverage"].denominator == 1
        assert metrics["task_completion_evidence_coverage"].value == 0.0

    def test_approval_granted_and_tool_executed_is_completed_with_evidence(self):
        from evaluation.agent_eval.metrics import compute_metrics

        case = _hitl_case(expected_terminal_state="SUCCEEDED", expect_task_completed=True)
        obs = self._waiting_obs(
            case_id="hitl",
            terminal_state="SUCCEEDED",
            pending_actions=(),
            interrupt_payloads=(),
            response="退款已按您的审批结果执行完成。",
            resolution_status="resolved",
        )
        metrics = compute_metrics([case], [obs])
        assert metrics["task_completion_rate"].value == 1.0
        assert metrics["governance_outcome_match_rate"].value == 1.0
        assert metrics["task_completion_evidence_coverage"].denominator == 1

    def test_execution_failed_after_approval_is_not_completed(self):
        """审批通过但工具执行失败 -> 不得记为业务完成（重试/未知结果）。"""
        from evaluation.agent_eval.metrics import compute_metrics

        case = _hitl_case(expected_terminal_state="SUCCEEDED", expect_task_completed=True)
        obs = self._waiting_obs(
            case_id="hitl",
            terminal_state="SUCCEEDED",
            pending_actions=(),
            interrupt_payloads=(),
            response="抱歉，处理账单问题时遇到系统错误，请稍后重试。",
            fallback_markers=("抱歉，处理账单问题时遇到系统错误",),
        )
        metrics = compute_metrics([case], [obs])
        assert metrics["task_completion_rate"].value == 0.0
        assert metrics["task_completion_evidence_coverage"].status == "NOT_MEASURED"


class TestShippedDatasetHonoursCompletionLabels:
    """仓库数据集的标签必须与代码口径一致，否则分母说谎。"""

    def test_waiting_approval_cases_are_not_labelled_as_task_completed(self):
        from evaluation.agent_eval.cases import load_dataset

        offenders = [
            c.case_id
            for c in load_dataset().cases
            if c.expected_terminal_state == "WAITING_APPROVAL" and c.expect_task_completed
        ]
        assert not offenders, (
            f"这些 WAITING_APPROVAL case 被标成 expect_task_completed=true：{offenders}。"
            " 等审批期间业务动作没有执行，不能算业务完成。"
        )

    def test_fault_cases_are_not_labelled_as_task_completed(self):
        from evaluation.agent_eval.cases import load_dataset

        offenders = [
            c.case_id for c in load_dataset().cases if c.expect_fallback and c.expect_task_completed
        ]
        assert not offenders, f"故障注入 case 不能标成业务完成：{offenders}"
