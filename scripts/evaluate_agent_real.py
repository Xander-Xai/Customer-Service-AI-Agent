#!/usr/bin/env python3
"""Agent Eval — **real-provider lane** entry (``agent-eval-real-provider/v1``).

This is the *explicitly opt-in* sibling of ``scripts/evaluate_agent.py``. It runs
the real compiled graph against a real OpenAI-compatible provider, records one
row per case (route / mode / tools / args / response / latency / tokens / cost),
and stores its artifact in a **separate** directory so real-model numbers can
never be confused with the scripted-LLM regression lane.

Safety model (all four must hold before a single external request)
-----------------------------------------------------------------
1. a credential is present in the environment (never on the CLI);
2. ``AGENT_EVAL_REAL_PROVIDER_AUTHORIZED=1``;
3. the ``--i-authorize-external-calls`` flag is passed;
4. the plan prints *first* (model, host, query cap, token budget, cost cap).

If any is missing the lane prints the plan and writes a ``NOT_MEASURED`` artifact
— it makes **zero** network calls. There is no silent fallback to a mock.

Examples::

    # plan only / no credentials -> NOT_MEASURED, no network:
    python3 scripts/evaluate_agent_real.py

    # budgeted real run is blocked until graph metering is wired end-to-end.
    # See docs/reference/agent-evaluation.md for isolated runtime prerequisites.

    # an independently-reviewed business subset can be supplied via --dataset;
    # its provenance (human_confirmed) is reported, never fabricated.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evaluation.agent_eval.cases import DatasetError, load_dataset  # noqa: E402
from evaluation.agent_eval.real_provider import (  # noqa: E402
    API_KEY_ENVS,
    AUTHORIZATION_ENV,
    RealProviderBudgetExceeded,
    RealProviderNotAuthorized,
    estimate_request_volume,
    resolve_plan,
    run_real_provider_lane,
    validate_runtime_plan,
)

REPORT_SCHEMA_VERSION = "agent-eval-real-provider/v1"


def _git_sha() -> str | None:
    from core.code_provenance import collect_code_provenance

    return collect_code_provenance(REPO_ROOT).commit_sha


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _filter(cases, args):
    if args.case:
        wanted = set(args.case)
        return [c for c in cases if c.case_id in wanted]
    if args.tags:
        wanted = set(args.tags)
        return [c for c in cases if wanted & set(c.tags)]
    return list(cases)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--tags", action="append", default=[])
    parser.add_argument("--out", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-queries", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-tokens-per-request", type=int, default=1024)
    parser.add_argument("--max-total-tokens", type=int, default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--cost-per-1k-tokens", type=float, default=None)
    parser.add_argument(
        "--allow-unmetered-provider",
        action="store_true",
        help="explicitly allow a case-capped run without enforceable token/cost limits",
    )
    parser.add_argument(
        "--i-authorize-external-calls",
        action="store_true",
        help="explicit consent to contact the provider (required; the env switch "
        f"{AUTHORIZATION_ENV}=1 is also required)",
    )
    return parser


def _print_plan(plan, estimate, authorized_reason: str) -> None:
    red = plan.redacted()
    print("=== Real-provider plan (printed before any external call) ===")
    print(f"  model                : {red['model']}")
    print(f"  base_url_host        : {red['base_url_host']}")
    print(
        f"  credential_present   : {red['credential_present']} " f"(env only: {list(API_KEY_ENVS)})"
    )
    print(f"  authorized           : {red['authorized']}")
    if not red["authorized"]:
        print(f"    ↳ reason           : {authorized_reason}")
    print(f"  max_queries          : {red['max_queries']}")
    print(f"  concurrency          : {red['concurrency']}")
    print(f"  timeout_seconds      : {red['timeout_seconds']}")
    print(f"  max_tokens/request   : {red['max_tokens_per_request']} (not enforced end-to-end)")
    print(f"  max_total_tokens     : {red['max_total_tokens']}")
    print(f"  max_cost_usd         : {red['max_cost_usd']}")
    print(f"  cost_per_1k_tokens   : {red['cost_per_1k_tokens']}")
    print("--- planned case volume (provider spend NOT bounded) ---")
    print(f"  dataset_size         : {estimate['dataset_size']}")
    print(f"  effective_queries    : {estimate['effective_queries']}")
    print(f"  worst_case_tokens    : {estimate['worst_case_tokens']} (NOT_MEASURED)")
    print(f"  worst_case_cost_usd  : {estimate['worst_case_cost_usd']}")
    if estimate.get("cost_note"):
        print(f"  cost_note            : {estimate['cost_note']}")


def _authorized_reason(plan) -> str:
    if not plan.authorized:
        return "requires BOTH the --i-authorize-external-calls flag AND " f"{AUTHORIZATION_ENV}=1"
    if not plan.has_credential:
        return f"no credential in environment ({list(API_KEY_ENVS)})"
    return "authorized"


def _write_not_measured(out: Path, *, plan, estimate, dataset, reason: str) -> None:
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "NOT_MEASURED",
        "reason": reason,
        "git_sha": _git_sha(),
        "plan": plan.redacted(),
        "estimate": estimate,
        "dataset": {
            "path": dataset.path,
            "sha256": dataset.sha256,
            "case_count": len(dataset),
            "annotation": dataset.population_counts(),
        },
        "separate_from_scripted_lane": True,
        "provenance": {
            "real_provider": {
                "status": "NOT_MEASURED",
                "reason": reason,
                "network_calls_made": 0,
            },
            "scripted_lane_artifact": "artifacts/agent-eval/<ts>/report.json",
            "note": (
                "This artifact proves the real-provider lane is wired and guarded; "
                "it does NOT contain any real-model metric. Provide a credential and "
                "both authorization switches, then re-run."
            ),
        },
        "results": [],
        "summary": {"attempted": 0, "ok": 0, "error": 0},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


async def _run_authorized(plan, cases) -> dict:
    """Run the real graph (real provider LLM) for each case.

    The container is built with the configured provider; **no** scripted LLM is
    injected, so a real-model run can never be silently replaced by a mock.
    """
    from evaluation.agent_eval.harness import AgentEvalHarness
    from llm.client import OpenAICompatibleClient
    from llm.usage_scope import collect_usage

    # Startup may issue a paid health-check request; account separately.
    with collect_usage() as lifecycle:
        async with AgentEvalHarness(hitl_enabled=True, inject_scripted_llm=False) as harness:

            if not isinstance(harness.container.llm, OpenAICompatibleClient):
                raise RealProviderNotAuthorized("runtime did not initialize the expected chat client")

            async def _runner(case):
                with collect_usage() as meter:
                    obs = await harness.run_case(case)
                usage = meter.snapshot()
                return {
                    "route": obs.observed_route,
                    "mode": obs.observed_mode,
                    "tool_calls": [c.to_dict() for c in obs.tool_calls],
                    "response_nonempty": bool(obs.response.strip()),
                    "route_source": obs.route_source,
                    "terminal_state": obs.terminal_state,
                    "fallback_observed": bool(obs.fallback_markers),
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "chat_http_metering": usage,
                    "execution_error": bool(obs.error),
                }

            run = await run_real_provider_lane(plan=plan, cases=cases, case_runner=_runner)
    run["lifecycle_chat_http_metering"] = lifecycle.snapshot()
    return run


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        plan = resolve_plan(
            model=args.model,
            base_url=args.base_url,
            max_queries=args.max_queries,
            concurrency=args.concurrency,
            timeout_seconds=args.timeout,
            max_tokens_per_request=args.max_tokens_per_request,
            max_total_tokens=args.max_total_tokens,
            max_cost_usd=args.max_cost_usd,
            cost_per_1k_tokens=args.cost_per_1k_tokens,
            authorized=args.i_authorize_external_calls,
        )
    except RealProviderBudgetExceeded as exc:
        print(f"❌ invalid budget: {exc}", file=sys.stderr)
        return 2

    try:
        dataset = load_dataset(args.dataset)
    except DatasetError as exc:
        print(f"❌ dataset invalid: {exc}", file=sys.stderr)
        return 2

    cases = _filter(dataset.cases, args)
    estimate = estimate_request_volume(plan, len(cases))

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = (
        Path(args.out)
        if args.out
        else (REPO_ROOT / "artifacts" / "agent-eval-real" / stamp / "report.json")
    )

    if out.exists() or out.is_symlink():
        print("Output must be a new evidence file", file=sys.stderr)
        return 2

    _print_plan(plan, estimate, _authorized_reason(plan))

    if not (plan.authorized and plan.has_credential):
        reason = _authorized_reason(plan)
        _write_not_measured(out, plan=plan, estimate=estimate, dataset=dataset, reason=reason)
        print()
        print(f"status = NOT_MEASURED ({reason})")
        print("  -> no external request was made.")
        print(f"  -> artifact: {_display(out)}")
        return 0

    import core.config as runtime_config

    try:
        validate_runtime_plan(plan, runtime_config)
        if not args.allow_unmetered_provider:
            raise RealProviderNotAuthorized("explicit unmetered provider permission required")
    except RealProviderNotAuthorized as exc:
        _write_not_measured(out, plan=plan, estimate=estimate, dataset=dataset, reason=str(exc))
        return 2
    if not cases:
        _write_not_measured(
            out, plan=plan, estimate=estimate, dataset=dataset, reason="empty_selection"
        )
        return 2

    print("\nrunning the real graph against the real provider…")
    try:
        run = asyncio.run(_run_authorized(plan, cases))
    except RealProviderNotAuthorized as exc:  # defence in depth
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except RealProviderBudgetExceeded as exc:
        print(f"❌ budget: {exc}", file=sys.stderr)
        return 2

    complete = (
        run["summary"]["attempted"] == estimate["effective_queries"]
        and run["summary"]["error"] == 0
        and not run["budget_exceeded"]
    )
    status = "MEASURED" if complete else "INCOMPLETE"
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": status,
        "git_sha": _git_sha(),
        "plan": plan.redacted(),
        "estimate": estimate,
        "dataset": {
            "path": dataset.path,
            "sha256": dataset.sha256,
            "case_count": len(dataset),
            "annotation": dataset.population_counts(),
        },
        "separate_from_scripted_lane": True,
        "provenance": {
            "real_provider": {
                "status": status,
                "model": plan.model,
                "network_calls_made": None,
                "agent_cases_attempted": run["summary"]["attempted"],
            },
            "scripted_lane_artifact": "artifacts/agent-eval/<ts>/report.json",
            "note": (
                "Real-model results, separate from scripted replay. A graph may "
                "issue multiple provider calls per agent case; provider network-call "
                "count is NOT_MEASURED. Token/cost totals are only reported when "
                "the metering is complete for all attempted cases."
            ),
        },
        "results": run["results"],
        "summary": run["summary"],
        "trajectory": run["trajectory"],
        "lifecycle_chat_http_metering": run.get("lifecycle_chat_http_metering"),
        "budget_exceeded": run["budget_exceeded"],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print()
    print(
        f"status = {status}  attempted={run['summary']['attempted']} "
        f"ok={run['summary']['ok']} error={run['summary']['error']}"
    )
    if run["budget_exceeded"]:
        print(f"  ⚠️ budget exceeded: {run['budget_exceeded']}")
    print(f"  -> artifact: {_display(out)}")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
