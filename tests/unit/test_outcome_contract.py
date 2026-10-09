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
    def test_normal_answer_is_resolved_with_evidence(self):
        outcome = classify_outcome(
            {"response": "订单 RO-1 已于昨天发出，预计明天送达，请留意物流。"}
        )
        assert outcome.workflow_finished is True
        assert outcome.response_delivered is True
        assert outcome.degraded is False
        assert outcome.business_resolved is True
        assert outcome.outcome_verified is True
        assert outcome.requires_human_action is False
        assert resolution_status_for(outcome) == RESOLUTION_RESOLVED

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

    def test_hitl_approved_and_executed_can_be_resolved(self):
        state = {
            "response": "退款已按您的审批结果执行完成。",
            "approval_results": [{"tool": "staging_refund", "status": "executed", "result": "ok"}],
        }
        outcome = classify_outcome(state)
        assert outcome.requires_human_action is False
        assert outcome.business_resolved is True

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
        assert outcome.business_resolved is False

    def test_explicit_handoff_is_requires_human(self):
        state = {"response": "您的问题需要更高级别的处理，正在为您转接人工客服。"}
        outcome = classify_outcome(state)
        assert outcome.requires_human_action is True
        assert outcome.business_resolved is False

    def test_duplicate_task_historical_valid_result_is_resolved(self):
        # 缓存命中返回历史有效结果：交付有效、非降级 -> 仍算解决（缓存命中另计）。
        state = {"response": "订单 RO-1 已于昨天发出，预计明天送达。", "cached": True}
        outcome = classify_outcome(state)
        assert outcome.business_resolved is True
        assert outcome.outcome_verified is True

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
