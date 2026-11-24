"""Real-provider agent-eval lane: authorization, budget and per-case evidence.

These tests never touch the network. They prove the *guards* (which is what
protects against accidental spend) and the per-case evidence shape, using an
injected case runner.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from evaluation.agent_eval.real_provider import (
    AUTHORIZATION_ENV,
    RealProviderBudgetExceeded,
    RealProviderNotAuthorized,
    estimate_request_volume,
    resolve_plan,
    run_real_provider_lane,
    validate_runtime_plan,
)


def _authorized_plan(**overrides):
    params = dict(max_queries=10, cost_per_1k_tokens=None)
    params.update(overrides)
    # authorized=True + a credential injected directly (the env path is tested
    # separately); resolve_plan is the only env reader in production.
    plan = resolve_plan(
        authorized=True,
        env={AUTHORIZATION_ENV: "1", "AGENT_EVAL_REAL_PROVIDER_API_KEY": "sk-test"},
        **params,
    )
    return plan


class TestAuthorization:
    def test_env_switch_alone_is_not_authorized(self):
        plan = resolve_plan(authorized=False, env={AUTHORIZATION_ENV: "1"})
        assert plan.authorized is False

    def test_cli_flag_alone_is_not_authorized(self):
        plan = resolve_plan(authorized=True, env={AUTHORIZATION_ENV: "0"})
        assert plan.authorized is False

    def test_both_switches_authorize(self):
        plan = resolve_plan(authorized=True, env={AUTHORIZATION_ENV: "true"})
        assert plan.authorized is True

    def test_credential_read_from_env_only_and_never_in_redacted_plan(self):
        plan = resolve_plan(env={"OPENAI_API_KEY": "sk-secret-value"})
        assert plan.api_key == "sk-secret-value"
        assert "sk-secret-value" not in json.dumps(plan.redacted())
        assert plan.redacted()["credential_present"] is True

    def test_lane_requires_authorization(self):
        plan = _authorized_plan()
        unauthorized = resolve_plan(env={}, authorized=False)
        with pytest.raises(RealProviderNotAuthorized):
            asyncio.run(
                run_real_provider_lane(plan=unauthorized, cases=[], case_runner=lambda c: None)
            )
        assert plan.authorized  # sanity

    def test_lane_requires_credential(self):
        plan = resolve_plan(authorized=True, env={AUTHORIZATION_ENV: "1"})
        with pytest.raises(RealProviderNotAuthorized):
            asyncio.run(run_real_provider_lane(plan=plan, cases=[], case_runner=lambda c: None))

    def test_invalid_budget_rejected(self):
        with pytest.raises(RealProviderBudgetExceeded):
            resolve_plan(max_queries=0)
        with pytest.raises(RealProviderBudgetExceeded):
            resolve_plan(concurrency=0)


class _Case:
    def __init__(self, case_id):
        self.case_id = case_id


class TestRunLane:
    def test_records_per_case_and_provider_error_without_fallback(self):
        plan = _authorized_plan()

        async def _runner(case):
            if case.case_id == "boom":
                raise RuntimeError("provider 500")
            return {
                "route": "billing",
                "mode": "react",
                "tool_calls": [{"name": "staging_refund", "arguments": {"amount": 10}}],
                "response_nonempty": True,
            }

        run = asyncio.run(
            run_real_provider_lane(
                plan=plan, cases=[_Case("ok"), _Case("boom")], case_runner=_runner
            )
        )
        assert run["summary"] == {
            "attempted": 2,
            "ok": 1,
            "error": 1,
            "tokens_used": None,
            "cost_used_usd": None,
            "provider_request_count": None,
        }
        results = {r["case_id"]: r for r in run["results"]}
        assert results["ok"]["status"] == "OK"
        assert results["ok"]["tool_names"] == ["staging_refund"]
        assert results["ok"]["route"] == "billing"
        # The failure is recorded as evidence, not swallowed and not retried
        # against a mock.
        assert results["boom"]["status"] == "ERROR"
        assert results["boom"]["error"] == "RuntimeError"
        assert "provider 500" not in json.dumps(run)

    def test_max_queries_caps_external_calls(self):
        plan = _authorized_plan(max_queries=2)
        seen = []

        async def _runner(case):
            seen.append(case.case_id)
            return {"response_nonempty": True}

        run = asyncio.run(
            run_real_provider_lane(
                plan=plan,
                cases=[_Case(f"c{i}") for i in range(5)],
                case_runner=_runner,
            )
        )
        assert len(seen) == 2, "must not exceed max_queries"
        assert run["summary"]["attempted"] == 2

    def test_token_budget_stops_run(self):
        plan = _authorized_plan(max_queries=10, max_total_tokens=100)
        calls = []

        async def _runner(case):
            calls.append(case.case_id)
            return {"input_tokens": 60, "output_tokens": 60, "response_nonempty": True}

        run = asyncio.run(
            run_real_provider_lane(
                plan=plan, cases=[_Case(f"c{i}") for i in range(5)], case_runner=_runner
            )
        )
        # After the first case tokens_used=120 >= 100, so the second is blocked.
        assert len(calls) == 1
        assert run["budget_exceeded"] == "max_total_tokens"

    def test_cost_cap_uses_configured_price(self):
        plan = _authorized_plan(max_queries=10, cost_per_1k_tokens=1.0, max_cost_usd=0.05)
        calls = []

        async def _runner(case):
            calls.append(case.case_id)
            return {"input_tokens": 40, "output_tokens": 40, "response_nonempty": True}

        run = asyncio.run(
            run_real_provider_lane(
                plan=plan, cases=[_Case(f"c{i}") for i in range(5)], case_runner=_runner
            )
        )
        # 80 tokens @ $1/1k = $0.08 > $0.05 cap -> stop after one case.
        assert len(calls) == 1
        assert run["budget_exceeded"] == "max_cost_usd"


class TestEstimate:
    def test_estimate_is_upper_bound_and_caps_at_max_queries(self):
        plan = _authorized_plan(max_queries=10, max_tokens_per_request=100)
        est = estimate_request_volume(plan, dataset_size=50)
        assert est["effective_queries"] == 10
        assert est["provider_request_upper_bound"] is None
        assert est["worst_case_tokens"] is None
        assert est["per_request_token_cap_enforced"] is False
        assert est["per_request_token_cap"] == 100

    def test_configured_rate_does_not_create_a_fake_cost_upper_bound(self):
        plan = _authorized_plan(cost_per_1k_tokens=0.01)
        estimate = estimate_request_volume(plan, dataset_size=4)
        assert estimate["worst_case_tokens"] is None
        assert estimate["worst_case_cost_usd"] is None
        assert estimate["per_request_token_cap_enforced"] is False

    def test_cost_estimate_none_without_price(self):
        plan = _authorized_plan(cost_per_1k_tokens=None)
        est = estimate_request_volume(plan, dataset_size=5)
        assert est["worst_case_cost_usd"] is None
        assert "cost_note" in est


def test_api_key_env_priority():
    from evaluation.agent_eval import real_provider as rp

    assert rp.API_KEY_ENVS[0] == "AGENT_EVAL_REAL_PROVIDER_API_KEY"
    plan = resolve_plan(
        env={
            "AGENT_EVAL_REAL_PROVIDER_API_KEY": "lane-key",
            "OPENAI_API_KEY": "prod-key",
        }
    )
    assert plan.api_key == "lane-key"


class TestCliGuard:
    def test_unauthorized_cli_writes_not_measured_and_makes_no_call(self, tmp_path, monkeypatch):
        import scripts.evaluate_agent_real as cli

        monkeypatch.delenv(AUTHORIZATION_ENV, raising=False)
        out = tmp_path / "report.json"
        rc = cli.main(["--out", str(out), "--max-queries", "1"])
        assert rc == 0
        report = json.loads(out.read_text())
        assert report["status"] == "NOT_MEASURED"
        assert report["provenance"]["real_provider"]["network_calls_made"] == 0
        assert report["results"] == []

    def test_cli_flag_without_env_switch_is_still_not_measured(self, tmp_path, monkeypatch):
        import scripts.evaluate_agent_real as cli

        monkeypatch.delenv(AUTHORIZATION_ENV, raising=False)
        out = tmp_path / "report.json"
        rc = cli.main(["--i-authorize-external-calls", "--out", str(out)])
        assert rc == 0
        assert json.loads(out.read_text())["status"] == "NOT_MEASURED"


def _matching_runtime(plan):
    return SimpleNamespace(
        OPENAI_MODEL=plan.model,
        OPENAI_BASE_URL=plan.base_url,
        OPENAI_API_KEY=plan.api_key,
        evaluate_llm_api_key=lambda key: SimpleNamespace(usable=True),
        ERP_MODE="mock",
        MCP_ENABLED=False,
        MULTIMODAL_ENABLED=False,
        EMBEDDING_PROVIDER="local",
        RERANKER_PROVIDER="local",
    )


def test_runtime_preflight_accepts_only_matching_isolated_configuration():
    plan = _authorized_plan()
    validate_runtime_plan(plan, _matching_runtime(plan))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("OPENAI_MODEL", "wrong-model"),
        ("OPENAI_BASE_URL", "https://other.invalid"),
        ("OPENAI_API_KEY", "other-key"),
        ("ERP_MODE", "real"),
        ("MCP_ENABLED", True),
        ("MULTIMODAL_ENABLED", True),
        ("EMBEDDING_PROVIDER", "remote"),
        ("RERANKER_PROVIDER", "remote"),
    ],
)
def test_runtime_preflight_rejects_mismatch_or_nonisolated_services(field, value):
    plan = _authorized_plan()
    config = _matching_runtime(plan)
    setattr(config, field, value)
    with pytest.raises(RealProviderNotAuthorized):
        validate_runtime_plan(plan, config)


def test_runtime_preflight_rejects_unenforceable_budget():
    plan = _authorized_plan(max_total_tokens=100)
    with pytest.raises(RealProviderNotAuthorized, match="metering"):
        validate_runtime_plan(plan, _matching_runtime(plan))


def test_url_credentials_never_appear_in_redacted_plan():
    plan = _authorized_plan(base_url="https://user:private-value@example.invalid/v1")
    assert "private-value" not in json.dumps(plan.redacted())
    with pytest.raises(RealProviderNotAuthorized, match="unsafe provider URL"):
        validate_runtime_plan(plan, _matching_runtime(plan))


def test_existing_evidence_is_not_overwritten(tmp_path, monkeypatch):
    import scripts.evaluate_agent_real as cli

    monkeypatch.delenv(AUTHORIZATION_ENV, raising=False)
    target = tmp_path / "report.json"
    target.write_text("original")
    assert cli.main(["--out", str(target)]) == 2
    assert target.read_text() == "original"


def test_authorized_cli_requires_explicit_unmetered_permission(tmp_path, monkeypatch):
    import scripts.evaluate_agent_real as cli

    monkeypatch.setenv(AUTHORIZATION_ENV, "1")
    monkeypatch.setenv("AGENT_EVAL_REAL_PROVIDER_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(cli, "validate_runtime_plan", lambda plan, config: None)

    async def forbidden(*args):
        pytest.fail("provider must not be called without unmetered permission")

    monkeypatch.setattr(cli, "_run_authorized", forbidden)
    target = tmp_path / "report.json"
    rc = cli.main(["--i-authorize-external-calls", "--out", str(target)])
    assert rc == 2
    report = json.loads(target.read_text())
    assert report["status"] == "NOT_MEASURED"
    assert report["provenance"]["real_provider"]["network_calls_made"] == 0


def test_trajectory_keeps_misroutes_errors_and_budget_stopped_cases_in_denominator():
    from evaluation.agent_eval.real_provider import RealCaseResult, summarize_trajectory

    def case(case_id, reviewed=True):
        return SimpleNamespace(
            case_id=case_id,
            annotation=SimpleNamespace(is_human_confirmed=reviewed),
            expected_route="order_status",
            expected_tools=("lookup",),
            expected_parameters={"lookup": {"id": 1}},
            expected_terminal_state="SUCCEEDED",
        )

    cases = [
        case("correct"),
        case("misroute"),
        case("error"),
        case("missing"),
        case("candidate", False),
    ]
    results = [
        RealCaseResult(
            case_id="correct",
            status="OK",
            route="order_status",
            tool_names=("lookup",),
            tool_calls=(
                {
                    "name": "lookup",
                    "arguments": {"id": 1},
                    "schema_errors": [],
                    "unknown_tool": False,
                },
            ),
            terminal_state="SUCCEEDED",
        ),
        RealCaseResult(
            case_id="misroute", status="OK", route="general", terminal_state="SUCCEEDED"
        ),
        RealCaseResult(case_id="error", status="ERROR"),
        RealCaseResult(case_id="candidate", status="OK", route="order_status"),
    ]
    report = summarize_trajectory(cases, results)
    route = report["metrics"]["reviewed_route_agreement"]
    assert (route["numerator"], route["denominator"], route["status"]) == (1, 4, "INCOMPLETE")
    assert report["metrics"]["candidate_tool_set_match"]["denominator"] == 5
    assert report["metrics"]["candidate_argument_match"]["numerator"] == 1
    assert report["not_attempted"] == ["missing"]
    assert report["business_completion_verified"] is False
    assert report["provider_transport_verified"] is False
    with pytest.raises(ValueError, match="identity"):
        summarize_trajectory(cases, results + [results[0]])


def test_trajectory_does_not_treat_bool_as_integer_or_unknown_schema_as_valid():
    from evaluation.agent_eval.real_provider import RealCaseResult, summarize_trajectory

    case = SimpleNamespace(case_id="case", expected_parameters={"lookup": {"id": 1}})
    result = RealCaseResult(
        case_id="case",
        status="OK",
        tool_calls=(
            {
                "name": "lookup",
                "arguments": {"id": True},
                "schema_errors": [],
                "unknown_tool": False,
            },
            {"name": "lookup", "arguments": {"id": 1}},
        ),
    )
    assert (
        summarize_trajectory([case], [result])["metrics"]["candidate_argument_match"]["value"] == 0
    )
