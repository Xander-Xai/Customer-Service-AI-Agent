"""Tests for the Agent Eval expected-parameter diagnostic (dataset schema + metric).

These are offline and deterministic: no graph, no Qdrant, no provider. They pin
two things that are easy to silently break:

* the dataset schema rejects an ``expected_parameters`` block that could never be
  observed (no ``expected_tools`` / no ``scripted_tool_calls``);
* ``expected_parameter_match_rate`` actually discriminates — a control case whose
  scripted arguments disagree with ``expected_parameters`` must lower it.
"""

from __future__ import annotations

import pytest

from evaluation.agent_eval.cases import DatasetError, parse_case
from evaluation.agent_eval.harness import CaseObservation, ToolCallObservation
from evaluation.agent_eval.metrics import compute_metrics

pytestmark = pytest.mark.unit

SV = "agent-eval-dataset/v1"


def _payload(**overrides):
    payload = {
        "schema_version": SV,
        "case_id": "c1",
        "input": "查询订单状态",
        "expected_terminal_state": "SUCCEEDED",
        "max_steps": 12,
        "expected_tools": ["staging_readonly_lookup"],
        "scripted_tool_calls": [
            {"name": "staging_readonly_lookup", "arguments": {"order_id": "RO-1"}}
        ],
        "expected_parameters": {"staging_readonly_lookup": {"order_id": "RO-1"}},
    }
    payload.update(overrides)
    return payload


class TestExpectedParametersSchema:
    def test_valid_expected_parameters_accepted(self):
        case = parse_case(_payload(), 1)
        assert case.expected_parameters == {"staging_readonly_lookup": {"order_id": "RO-1"}}
        assert case.scores_parameters() is True

    def test_expected_parameters_without_expected_tools_rejected(self):
        with pytest.raises(DatasetError, match="without expected_tools"):
            parse_case(_payload(expected_tools=[], scripted_tool_calls=[]), 1)

    def test_expected_parameters_for_unknown_tool_rejected(self):
        with pytest.raises(DatasetError, match="not in expected_tools"):
            parse_case(
                _payload(expected_parameters={"other_tool": {"x": 1}}),
                1,
            )

    def test_expected_parameters_must_be_object(self):
        with pytest.raises(DatasetError, match="must be an object"):
            parse_case(_payload(expected_parameters=["not", "a", "dict"]), 1)

    def test_absent_expected_parameters_is_empty(self):
        case = parse_case(_payload(expected_parameters=None), 1)
        assert case.expected_parameters == {}
        assert case.scores_parameters() is False


def _obs(case_id: str, tool_calls: tuple[ToolCallObservation, ...]) -> CaseObservation:
    return CaseObservation(
        case_id=case_id,
        nodes_executed=("agent_execution",),
        step_count=3,
        observed_route="order_status",
        route_source="llm_arbitrated",
        rule_route=None,
        rule_confidence=0.0,
        router_llm_calls=1,
        observed_mode="react",
        resolution_status="resolved",
        response="done",
        terminal_state="SUCCEEDED",
        tool_calls=tool_calls,
        fallback_markers=(),
        route_shortcut_used=False,
        hitl_defer_logged=False,
        pending_actions=(),
        interrupt_payloads=(),
        side_effect_counters={},
        scripted_plan_remaining=0,
        scripted_tool_rounds=1,
        elapsed_ms=1.0,
    )


def _call(name: str, arguments: dict) -> ToolCallObservation:
    return ToolCallObservation(
        name=name,
        arguments=arguments,
        policy_risk="low",
        policy_requires_approval=False,
        schema_errors=(),
        executed=True,
        deferred=False,
    )


class TestExpectedParameterMatchRate:
    def _case(self, expected_order="RO-1"):
        return parse_case(
            _payload(expected_parameters={"staging_readonly_lookup": {"order_id": expected_order}}),
            1,
        )

    def test_matching_arguments_score_one(self):
        case = self._case("RO-1")
        metrics = compute_metrics(
            [case], [_obs("c1", (_call("staging_readonly_lookup", {"order_id": "RO-1"}),))]
        )
        metric = metrics["expected_parameter_match_rate"]
        assert metric.value == 1.0
        assert metric.denominator == 1

    def test_mismatching_arguments_score_zero(self):
        case = self._case("RO-1")
        metrics = compute_metrics(
            [case], [_obs("c1", (_call("staging_readonly_lookup", {"order_id": "RO-WRONG"}),))]
        )
        metric = metrics["expected_parameter_match_rate"]
        assert metric.value == 0.0
        assert metric.detail["misses"][0]["expected"] == {"order_id": "RO-1"}

    def test_fault_injection_excluded_not_scored_zero(self):
        case = parse_case(_payload(expect_fallback=True, notes="fault"), 1)
        metrics = compute_metrics([case], [_obs("c1", ())])
        metric = metrics["expected_parameter_match_rate"]
        # No non-fault slot remains -> NOT_MEASURED, never a 0% verdict.
        assert metric.value is None
        assert metric.status == "NOT_MEASURED"
        assert metric.excluded == 1
