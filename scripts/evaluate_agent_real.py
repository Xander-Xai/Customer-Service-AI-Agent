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

    # authorized run (requires AGENT_EVAL_REAL_PROVIDER_API_KEY + env switch):
    python3 scripts/evaluate_agent_real.py \
        --i-authorize-external-calls --model Qwen/Qwen3-8B \
        --max-queries 40 --max-total-tokens 200000

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
    print(f"  max_tokens/request   : {red['max_tokens_per_request']}")
    print(f"  max_total_tokens     : {red['max_total_tokens']}")
    print(f"  max_cost_usd         : {red['max_cost_usd']}")
    print(f"  cost_per_1k_tokens   : {red['cost_per_1k_tokens']}")
    print("--- estimated scale (upper bound) ---")
    print(f"  dataset_size         : {estimate['dataset_size']}")
    print(f"  effective_queries    : {estimate['effective_queries']}")
    print(f"  worst_case_tokens    : {estimate['worst_case_tokens']}")
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
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _run_authorized(plan, cases) -> dict:
    """Run the real graph (real provider LLM) for each case.

    The container is built with the configured provider; **no** scripted LLM is
    injected, so a real-model run can never be silently replaced by a mock.
    """
    from evaluation.agent_eval.harness import AgentEvalHarness

    async with AgentEvalHarness(hitl_enabled=True, inject_scripted_llm=False) as harness:

        async def _runner(case):
            obs = await harness.run_case(case)
            if obs.error:
                raise RuntimeError(obs.error)
            return {
                "route": obs.observed_route,
                "mode": obs.observed_mode,
                "tool_calls": [c.to_dict() for c in obs.tool_calls],
                "response_nonempty": bool(obs.response.strip()),
                # Token/cost are NOT_MEASURED unless the provider response
                # surfaces them through the app's client (it currently does not
                # persist per-call usage). Never estimated.
                "input_tokens": None,
                "output_tokens": None,
            }

        return await run_real_provider_lane(plan=plan, cases=cases, case_runner=_runner)


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

    _print_plan(plan, estimate, _authorized_reason(plan))

    if not (plan.authorized and plan.has_credential):
        reason = _authorized_reason(plan)
        _write_not_measured(out, plan=plan, estimate=estimate, dataset=dataset, reason=reason)
        print()
        print(f"status = NOT_MEASURED ({reason})")
        print("  -> no external request was made.")
        print(f"  -> artifact: {_display(out)}")
        return 0

    print("\nrunning the real graph against the real provider…")
    try:
        run = asyncio.run(_run_authorized(plan, cases))
    except RealProviderNotAuthorized as exc:  # defence in depth
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except RealProviderBudgetExceeded as exc:
        print(f"❌ budget: {exc}", file=sys.stderr)
        return 2

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "MEASURED",
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
                "status": "MEASURED",
                "model": plan.model,
                "network_calls_made": run["summary"]["attempted"],
            },
            "scripted_lane_artifact": "artifacts/agent-eval/<ts>/report.json",
            "note": (
                "Real-model results. Never blended with the scripted-LLM regression "
                "lane; token/cost are NOT_MEASURED unless the provider surfaces usage."
            ),
        },
        "results": run["results"],
        "summary": run["summary"],
        "budget_exceeded": run["budget_exceeded"],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print()
    print(
        f"status = MEASURED  attempted={run['summary']['attempted']} "
        f"ok={run['summary']['ok']} error={run['summary']['error']}"
    )
    if run["budget_exceeded"]:
        print(f"  ⚠️ budget exceeded: {run['budget_exceeded']}")
    print(f"  -> artifact: {_display(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
