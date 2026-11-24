"""Agent Eval — real-provider lane (explicitly opt-in, budget-guarded).

Why this is a **separate** module from :mod:`evaluation.agent_eval.harness`
-----------------------------------------------------------------------
The scripted-LLM harness measures *orchestration / governance* behaviour with a
deterministic in-process double. A real model measures something else entirely:
whether a provider-driven agent routes, calls tools and answers correctly. The
two must never be blended into one number (see
``docs/reference/agent-evaluation.md`` §3). This module is the only place a real
provider credential is ever read.

Non-negotiable guarantees
-------------------------
1. **Credentials only from the environment.** Never from a CLI flag, never from
   the dataset, never logged. :func:`resolve_plan` reads env vars only.
2. **Off by default.** Two independent switches must both be on before a single
   external request can happen: :data:`AUTHORIZATION_ENV` (env) and the
   ``--i-authorize-external-calls`` CLI flag. Either one missing → the lane
   returns ``NOT_MEASURED`` and makes **zero** network calls.
3. **Fail loud, never fall back.** If a case fails, it is recorded as ``ERROR``
   for that case. The runner never swaps in a mock/scripted model to keep a
   number looking good.
4. **Budgeted.** ``max_queries`` / ``max_total_tokens`` / ``max_cost_usd`` are
   checked *before* each case. Exhausting a budget stops the run and marks it
   ``BUDGET_EXCEEDED`` rather than silently overspending.

This module imports only the standard library so it can be unit-tested with no
provider, no network and no application container.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

#: Master authorization switch. Must be truthy **and** the CLI must pass the
#: explicit flag. Both are required so a stray env var cannot spend money.
AUTHORIZATION_ENV = "AGENT_EVAL_REAL_PROVIDER_AUTHORIZED"
#: Credential sources, in priority order. The lane-specific name wins so a
#: dedicated eval key can be scoped apart from production traffic.
API_KEY_ENVS: tuple[str, ...] = ("AGENT_EVAL_REAL_PROVIDER_API_KEY", "OPENAI_API_KEY")
BASE_URL_ENV = "AGENT_EVAL_REAL_PROVIDER_BASE_URL"
MODEL_ENV = "AGENT_EVAL_REAL_PROVIDER_MODEL"

DEFAULT_MODEL = "Qwen/Qwen3-8B"
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"

#: Per-case status vocabulary (kept tiny and explicit).
STATUS_OK = "OK"
STATUS_ERROR = "ERROR"

_TRUE = {"1", "true", "yes", "on"}


class RealProviderNotAuthorized(RuntimeError):
    """Raised when the lane is invoked without credential + explicit consent."""


class RealProviderBudgetExceeded(RuntimeError):
    """Raised if a run is attempted with a nonsensical budget."""


def _env_flag(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


def _resolve_api_key(env: Mapping[str, str]) -> str | None:
    for name in API_KEY_ENVS:
        raw = (env.get(name) or "").strip()
        if raw:
            return raw
    return None


@dataclass(frozen=True)
class RealProviderPlan:
    """Everything needed to execute (and to *print before* executing) a run.

    ``api_key`` is deliberately excluded from :meth:`redacted` so the plan can
    be printed and embedded in an artifact without leaking the credential.
    """

    model: str
    base_url: str
    api_key: str | None
    max_queries: int
    concurrency: int
    timeout_seconds: float
    max_tokens_per_request: int
    max_total_tokens: int | None
    max_cost_usd: float | None
    cost_per_1k_tokens: float | None
    authorized: bool

    @property
    def has_credential(self) -> bool:
        return bool(self.api_key)

    def redacted(self) -> dict[str, Any]:
        """Serializable plan for printing / artifact embedding (no secret)."""
        host = ""
        try:
            host = urlsplit(self.base_url).hostname or ""
        except Exception:  # pragma: no cover - defensive
            host = ""
        return {
            "model": self.model,
            "base_url_host": host,
            "credential_present": self.has_credential,
            "authorized": self.authorized,
            "max_queries": self.max_queries,
            "concurrency": self.concurrency,
            "timeout_seconds": self.timeout_seconds,
            "max_tokens_per_request": self.max_tokens_per_request,
            "max_total_tokens": self.max_total_tokens,
            "max_cost_usd": self.max_cost_usd,
            "cost_per_1k_tokens": self.cost_per_1k_tokens,
        }


def resolve_plan(
    *,
    model: str | None = None,
    base_url: str | None = None,
    max_queries: int = 50,
    concurrency: int = 1,
    timeout_seconds: float = 30.0,
    max_tokens_per_request: int = 1024,
    max_total_tokens: int | None = None,
    max_cost_usd: float | None = None,
    cost_per_1k_tokens: float | None = None,
    authorized: bool = False,
    env: Mapping[str, str] | None = None,
) -> RealProviderPlan:
    """Build a plan from env + CLI values. **The only credential read point.**"""
    env = env if env is not None else os.environ
    effective_max_queries = int(max_queries)
    if effective_max_queries <= 0:
        raise RealProviderBudgetExceeded("max_queries must be a positive integer")
    if concurrency <= 0:
        raise RealProviderBudgetExceeded("concurrency must be a positive integer")
    if timeout_seconds <= 0:
        raise RealProviderBudgetExceeded("timeout_seconds must be positive")
    if max_tokens_per_request <= 0:
        raise RealProviderBudgetExceeded("max_tokens_per_request must be positive")
    if max_total_tokens is not None and max_total_tokens <= 0:
        raise RealProviderBudgetExceeded("max_total_tokens must be positive when set")

    for name, value in (("max_cost_usd", max_cost_usd), ("cost_per_1k_tokens", cost_per_1k_tokens)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise RealProviderBudgetExceeded(f"{name} must be finite and positive")
    if not math.isfinite(timeout_seconds):
        raise RealProviderBudgetExceeded("timeout_seconds must be finite")

    env_authorized = _env_flag(env.get(AUTHORIZATION_ENV))
    return RealProviderPlan(
        model=(model or env.get(MODEL_ENV) or DEFAULT_MODEL).strip(),
        base_url=(base_url or env.get(BASE_URL_ENV) or DEFAULT_BASE_URL).strip(),
        api_key=_resolve_api_key(env),
        max_queries=effective_max_queries,
        concurrency=int(concurrency),
        timeout_seconds=float(timeout_seconds),
        max_tokens_per_request=int(max_tokens_per_request),
        max_total_tokens=max_total_tokens,
        max_cost_usd=max_cost_usd,
        cost_per_1k_tokens=cost_per_1k_tokens,
        # Both switches required.
        authorized=bool(authorized and env_authorized),
    )


def validate_runtime_plan(plan: RealProviderPlan, config: Any) -> None:
    """Fail closed before initializing a graph that could contact external services."""
    url = urlsplit(plan.base_url)
    if (
        not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})
        )
    ):
        raise RealProviderNotAuthorized("unsafe provider URL")
    if (
        plan.model != config.OPENAI_MODEL
        or config.OPENAI_BASE_URL.rstrip("/") != plan.base_url.rstrip("/")
        or plan.api_key != config.OPENAI_API_KEY
        or not config.evaluate_llm_api_key(config.OPENAI_API_KEY).usable
    ):
        raise RealProviderNotAuthorized("evaluation plan does not match runtime LLM configuration")
    if (
        config.ERP_MODE != "mock"
        or config.MCP_ENABLED
        or config.MULTIMODAL_ENABLED
        or config.EMBEDDING_PROVIDER != "local"
        or config.RERANKER_PROVIDER != "local"
    ):
        raise RealProviderNotAuthorized(
            "evaluation requires isolated mock ERP and local retrieval providers"
        )
    # This graph does not expose complete provider usage to the lane. Refuse
    # a promised token/cost budget that cannot be enforced even for the first case.
    if plan.max_total_tokens is not None or plan.max_cost_usd is not None:
        raise RealProviderNotAuthorized("graph token/cost metering is not wired end-to-end")


def estimate_request_volume(plan: RealProviderPlan, dataset_size: int) -> dict[str, Any]:
    """Report a bounded case count without inventing a provider-call upper bound.

    The compiled Agent graph can make multiple model calls per case. The CLI's
    max_tokens_per_request field is not currently wired to all provider calls,
    so multiplying case count by that field is *not* a token or cost ceiling.
    """
    effective = min(max(0, dataset_size), plan.max_queries)
    return {
        "dataset_size": dataset_size,
        "effective_queries": effective,
        "summarization": f"at most {effective} agent cases; provider call count not bounded",
        "provider_request_upper_bound": None,
        "worst_case_tokens": None,
        "per_request_token_cap": plan.max_tokens_per_request,
        "per_request_token_cap_enforced": False,
        "worst_case_cost_usd": None,
        "cost_note": (
            "The real graph can call the provider multiple times per case; "
            "per-request token cap and total usage are not wired end-to-end. "
            "No defensible pre-run cost upper bound is available."
        ),
    }


@dataclass
class RealCaseResult:
    """Per-case evidence for one real-model run. Contains no secrets."""

    case_id: str
    status: str
    route: str | None = None
    mode: str | None = None
    tool_names: tuple[str, ...] = ()
    tool_calls: tuple[dict[str, Any], ...] = ()
    response_nonempty: bool = False
    route_source: str | None = None
    terminal_state: str | None = None
    fallback_observed: bool | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "route": self.route,
            "mode": self.mode,
            "tool_names": list(self.tool_names),
            "tool_calls": list(self.tool_calls),
            "response_nonempty": self.response_nonempty,
            "route_source": self.route_source,
            "terminal_state": self.terminal_state,
            "fallback_observed": self.fallback_observed,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "error": self.error,
        }


#: A case runner receives the case and returns a normalized outcome mapping.
#: Recognized keys: ``route`` / ``mode`` / ``tool_names`` / ``tool_calls`` /
#: ``response_nonempty`` / ``input_tokens`` / ``output_tokens``. Anything missing
#: is recorded as NOT_MEASURED (``None``) — never faked.
CaseRunner = Callable[[Any], Awaitable[Mapping[str, Any]]]


def _coerce_tools(raw: Any) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    names: list[str] = []
    calls: list[dict[str, Any]] = []
    for item in raw or ():
        if isinstance(item, Mapping):
            name = str(item.get("name") or item.get("tool") or "")
            if name:
                names.append(name)
            calls.append(dict(item))
        elif isinstance(item, str):
            names.append(item)
            calls.append({"name": item})
    return tuple(names), tuple(calls)


def summarize_trajectory(cases: Sequence[Any], results: Sequence[RealCaseResult]) -> dict[str, Any]:
    """Score planned cases, never just the successful subset.

    Only route labels have an existing human-review workflow. Tool/argument
    and terminal expectations are candidate-contract diagnostics, not gold
    business accuracy. The observations do not attest provider transport.
    """
    # Preserve the existing injected-runner contract for string case IDs.
    planned_ids = [str(getattr(case, "case_id", None) or case) for case in cases]
    by_id = {result.case_id: result for result in results}
    if (
        len(set(planned_ids)) != len(planned_ids)
        or len(by_id) != len(results)
        or not set(by_id).issubset(planned_ids)
    ):
        raise ValueError("duplicate or unexpected trajectory case identity")

    checks: dict[str, list[dict[str, Any]]] = {
        "reviewed_route_agreement": [],
        "candidate_tool_set_match": [],
        "candidate_argument_match": [],
        "candidate_governance_state_match": [],
    }

    def add(name, case_id, result, matched):
        if result is None:
            outcome = "NOT_ATTEMPTED"
        elif result.status != STATUS_OK:
            outcome = "EXECUTION_ERROR"
        elif matched is None:
            outcome = "MISSING_OBSERVATION"
        else:
            outcome = "MATCH" if matched else "MISMATCH"
        checks[name].append({"case_id": case_id, "outcome": outcome})

    for case, case_id in zip(cases, planned_ids, strict=True):
        result = by_id.get(case_id)
        if getattr(case, "expect_fallback", False) or getattr(case, "scripted_failure", None):
            # Fault injection belongs to the scripted lane, not real-provider quality.
            continue
        if getattr(case, "expected_route", None) is not None and getattr(
            getattr(case, "annotation", None), "is_human_confirmed", False
        ):
            add(
                "reviewed_route_agreement",
                case_id,
                result,
                None
                if result is None or result.route is None
                else (result.route == case.expected_route),
            )
        expected = set(getattr(case, "expected_tools", ()))
        if expected:
            add(
                "candidate_tool_set_match",
                case_id,
                result,
                None if result is None else set(result.tool_names) == expected,
            )
        parameters = getattr(case, "expected_parameters", {})
        if parameters:
            matched = result is not None and all(
                any(
                    call.get("name") == name
                    and isinstance(call.get("arguments"), dict)
                    and all(
                        key in call["arguments"]
                        and json.dumps(call["arguments"][key], sort_keys=True)
                        == json.dumps(value, sort_keys=True)
                        for key, value in arguments.items()
                    )
                    and call.get("schema_errors") == []
                    and call.get("unknown_tool") is False
                    for call in result.tool_calls
                )
                for name, arguments in parameters.items()
            )
            add("candidate_argument_match", case_id, result, matched)
        if getattr(case, "expected_terminal_state", None) is not None:
            add(
                "candidate_governance_state_match",
                case_id,
                result,
                None
                if result is None or result.terminal_state is None
                else (result.terminal_state == case.expected_terminal_state),
            )

    metrics = {}
    for name, items in checks.items():
        hits = sum(item["outcome"] == "MATCH" for item in items)
        metrics[name] = {
            "numerator": hits,
            "denominator": len(items),
            "value": hits / len(items) if items else None,
            "status": "OBSERVED_AGREEMENT"
            if items
            and all(
                item["outcome"] not in {"NOT_ATTEMPTED", "EXECUTION_ERROR", "MISSING_OBSERVATION"}
                for item in items
            )
            else "INCOMPLETE"
            if items
            else "NOT_MEASURED",
            "cases": items,
        }
    return {
        "planned": len(cases),
        "attempted": len(results),
        "not_attempted": [case_id for case_id in planned_ids if case_id not in by_id],
        "fault_fixture_excluded": [
            case_id
            for case, case_id in zip(cases, planned_ids, strict=True)
            if getattr(case, "expect_fallback", False) or getattr(case, "scripted_failure", None)
        ],
        "metrics": metrics,
        "business_completion_verified": False,
        "provider_transport_verified": False,
        "tool_and_terminal_labels_independently_reviewed": False,
    }


async def run_real_provider_lane(
    *,
    plan: RealProviderPlan,
    cases: Sequence[Any],
    case_runner: CaseRunner,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Execute up to ``plan.max_queries`` cases against the real provider.

    The runner is injected so the lane is fully unit-testable with no network.
    Guards (authorization, credential, budget) are enforced here as well as in
    the CLI: defence in depth — a direct caller cannot bypass them.
    """
    if not plan.authorized:
        raise RealProviderNotAuthorized(
            f"real-provider lane requires authorization ({AUTHORIZATION_ENV}=1 "
            "and the explicit --i-authorize-external-calls flag)"
        )
    if not plan.has_credential:
        raise RealProviderNotAuthorized(
            "real-provider lane has no credential; set "
            f"one of {list(API_KEY_ENVS)} (never passed on the CLI)"
        )

    results: list[RealCaseResult] = []
    tokens_used = 0
    cost_used = 0.0
    cost_observed = False
    token_usage_complete = True
    cost_usage_complete = True
    budget_reason: str | None = None

    planned = list(cases)[: plan.max_queries]
    for case in planned:
        if plan.max_total_tokens is not None and tokens_used >= plan.max_total_tokens:
            budget_reason = "max_total_tokens"
            break
        if plan.max_cost_usd is not None and cost_used >= plan.max_cost_usd:
            budget_reason = "max_cost_usd"
            break

        case_id = str(getattr(case, "case_id", None) or case)
        started = clock()
        try:
            outcome = await case_runner(case)
        except Exception as exc:  # noqa: BLE001 - a failing case is evidence, not a crash
            token_usage_complete = False
            cost_usage_complete = False
            results.append(
                RealCaseResult(
                    case_id=case_id,
                    status=STATUS_ERROR,
                    latency_ms=round((clock() - started) * 1000, 3),
                    error=type(exc).__name__,
                )
            )
            # The failed provider call may still have consumed tokens/cost.
            # With an active budget, unknown usage cannot authorize a retry.
            if plan.max_total_tokens is not None or plan.max_cost_usd is not None:
                budget_reason = "provider_usage_unknown_after_error"
                break
            continue

        names, calls = _coerce_tools(outcome.get("tool_calls") or outcome.get("tool_names"))
        in_tok = outcome.get("input_tokens")
        out_tok = outcome.get("output_tokens")
        cost = outcome.get("cost_usd")
        # A provider might return missing, negative, non-finite or bool values.
        # None of those values is trustworthy metering evidence.
        input_is_metered = isinstance(in_tok, int) and not isinstance(in_tok, bool) and in_tok >= 0
        output_is_metered = (
            isinstance(out_tok, int) and not isinstance(out_tok, bool) and out_tok >= 0
        )
        usage_is_metered = input_is_metered and output_is_metered
        if input_is_metered:
            tokens_used += in_tok
        if output_is_metered:
            tokens_used += out_tok
        numeric_cost = isinstance(cost, int | float) and not isinstance(cost, bool)
        cost_is_metered = numeric_cost and math.isfinite(cost) and cost >= 0
        if cost_is_metered:
            cost_used += float(cost)
            cost_observed = True
        elif plan.cost_per_1k_tokens is not None and usage_is_metered:
            derived = (in_tok + out_tok) / 1000.0 * plan.cost_per_1k_tokens
            if math.isfinite(derived) and derived >= 0:
                cost_used += derived
                cost = round(derived, 6)
                cost_is_metered = True
                cost_observed = True

        # A limit whose usage source is missing cannot be enforced. Stop the
        # lane after this case rather than sending more unmetered requests.
        # This is not a pre-call hard cap: one already-started request may exceed
        # a budget. The real graph currently does not expose per-call usage.
        if not usage_is_metered:
            token_usage_complete = False
        if not cost_is_metered:
            cost_usage_complete = False
        if plan.max_total_tokens is not None and not usage_is_metered:
            budget_reason = "provider_token_usage_unavailable"
        if plan.max_cost_usd is not None and not cost_is_metered:
            budget_reason = "provider_cost_usage_unavailable"

        results.append(
            RealCaseResult(
                case_id=case_id,
                status=STATUS_OK,
                route=outcome.get("route"),
                mode=outcome.get("mode"),
                tool_names=names,
                tool_calls=calls,
                response_nonempty=bool(outcome.get("response_nonempty")),
                route_source=outcome.get("route_source"),
                terminal_state=outcome.get("terminal_state"),
                fallback_observed=outcome.get("fallback_observed"),
                latency_ms=round((clock() - started) * 1000, 3),
                input_tokens=in_tok if input_is_metered else None,
                output_tokens=out_tok if output_is_metered else None,
                cost_usd=cost if cost_is_metered else None,
            )
        )
        if budget_reason is not None:
            break

    ok = sum(1 for r in results if r.status == STATUS_OK)
    return {
        "plan": plan.redacted(),
        "results": [r.to_dict() for r in results],
        "trajectory": summarize_trajectory(planned, results),
        "summary": {
            "attempted": len(results),
            "ok": ok,
            "error": len(results) - ok,
            "tokens_used": tokens_used if token_usage_complete and results else None,
            "cost_used_usd": round(cost_used, 6) if cost_usage_complete and cost_observed else None,
            "provider_request_count": None,
        },
        "budget_exceeded": budget_reason,
    }


__all__ = [
    "API_KEY_ENVS",
    "AUTHORIZATION_ENV",
    "BASE_URL_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "MODEL_ENV",
    "STATUS_ERROR",
    "STATUS_OK",
    "RealCaseResult",
    "RealProviderBudgetExceeded",
    "RealProviderNotAuthorized",
    "RealProviderPlan",
    "estimate_request_volume",
    "resolve_plan",
    "validate_runtime_plan",
    "run_real_provider_lane",
]
