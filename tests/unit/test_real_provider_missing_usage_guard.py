"""Missing provider usage must not silently bypass real-agent evaluation budgets.

The runner is an injected local double: zero provider calls and zero credentials.
"""

import asyncio

from evaluation.agent_eval.real_provider import (
    AUTHORIZATION_ENV,
    resolve_plan,
    run_real_provider_lane,
)


def _plan(**kwargs):
    return resolve_plan(
        authorized=True,
        env={AUTHORIZATION_ENV: "1", "AGENT_EVAL_REAL_PROVIDER_API_KEY": "local-test-only"},
        max_queries=5,
        **kwargs,
    )


def test_token_budget_stops_on_missing_provider_usage():
    calls = []

    async def runner(case):
        calls.append(case)
        return {"route": "billing", "response_nonempty": True}

    report = asyncio.run(
        run_real_provider_lane(
            plan=_plan(max_total_tokens=500),
            cases=["first", "second", "third"],
            case_runner=runner,
        )
    )
    assert calls == ["first"]
    assert report["budget_exceeded"] == "provider_token_usage_unavailable"
    assert report["summary"]["attempted"] == 1
    assert report["summary"]["tokens_used"] is None
    assert report["results"][0]["input_tokens"] is None


def test_cost_budget_stops_if_no_cost_or_token_accounting():
    calls = []

    async def runner(case):
        calls.append(case)
        return {"response_nonempty": True}

    report = asyncio.run(
        run_real_provider_lane(
            plan=_plan(max_cost_usd=0.10, cost_per_1k_tokens=0.20),
            cases=["first", "second"],
            case_runner=runner,
        )
    )
    assert calls == ["first"]
    assert report["budget_exceeded"] == "provider_cost_usage_unavailable"
    assert report["summary"]["attempted"] == 1


def test_cost_budget_with_observed_tokens_can_continue():
    calls = []

    async def runner(case):
        calls.append(case)
        return {"input_tokens": 10, "output_tokens": 10, "response_nonempty": True}

    report = asyncio.run(
        run_real_provider_lane(
            plan=_plan(max_cost_usd=0.10, cost_per_1k_tokens=0.20),
            cases=["first", "second"],
            case_runner=runner,
        )
    )
    assert calls == ["first", "second"]
    assert report["budget_exceeded"] is None


def test_unbudgeted_replay_can_record_unknown_usage_without_faking_cost():
    calls = []

    async def runner(case):
        calls.append(case)
        return {"response_nonempty": True}

    report = asyncio.run(
        run_real_provider_lane(
            plan=_plan(),
            cases=["first", "second"],
            case_runner=runner,
        )
    )
    assert calls == ["first", "second"]
    assert report["budget_exceeded"] is None
    assert all(row["cost_usd"] is None for row in report["results"])
    assert report["summary"]["tokens_used"] is None
    assert report["summary"]["provider_request_count"] is None


def test_budgeted_provider_error_does_not_trigger_another_billable_case():
    calls = []

    async def runner(case):
        calls.append(case)
        raise RuntimeError("provider connection interrupted")

    report = asyncio.run(
        run_real_provider_lane(
            plan=_plan(max_total_tokens=500),
            cases=["first", "second"],
            case_runner=runner,
        )
    )
    assert calls == ["first"]
    assert report["budget_exceeded"] == "provider_usage_unknown_after_error"
    assert report["results"][0]["status"] == "ERROR"


def test_explicit_provider_cost_without_rate_card_is_accounted():
    async def runner(case):
        return {"cost_usd": 0.012, "response_nonempty": True}

    report = asyncio.run(
        run_real_provider_lane(
            plan=_plan(max_cost_usd=0.10),
            cases=["first"],
            case_runner=runner,
        )
    )
    assert report["summary"]["cost_used_usd"] == 0.012
    assert report["summary"]["tokens_used"] is None
    assert report["results"][0]["cost_usd"] == 0.012
    assert report["budget_exceeded"] is None


def test_nonfinite_or_negative_usage_is_not_accepted_as_budget_evidence():
    async def runner(case):
        return {"input_tokens": -2, "output_tokens": 4, "cost_usd": float("nan")}

    report = asyncio.run(
        run_real_provider_lane(
            plan=_plan(max_total_tokens=100, max_cost_usd=0.1),
            cases=["first", "second"],
            case_runner=runner,
        )
    )
    assert report["summary"]["attempted"] == 1
    assert report["results"][0]["input_tokens"] is None
    assert report["results"][0]["cost_usd"] is None
    assert report["summary"]["cost_used_usd"] is None
    assert report["budget_exceeded"] == "provider_cost_usage_unavailable"
