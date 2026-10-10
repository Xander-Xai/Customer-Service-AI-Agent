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

import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
            from urllib.parse import urlparse

            host = urlparse(self.base_url).netloc
        except Exception:  # pragma: no cover - defensive
            host = self.base_url
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


def estimate_request_volume(plan: RealProviderPlan, dataset_size: int) -> dict[str, Any]:
    """Worst-case request / token estimate shown *before* any call is made.

    The estimate is deliberately an upper bound (one request per query at the
    per-request token cap). Real token use is usually lower; the point is that
    the operator sees the ceiling they are authorizing.
    """
    effective = min(max(0, dataset_size), plan.max_queries)
    worst_tokens = effective * plan.max_tokens_per_request
    estimate: dict[str, Any] = {
        "dataset_size": dataset_size,
        "effective_queries": effective,
        "summarization": (f"at most {effective} provider request(s) (one per query)"),
        "worst_case_tokens": worst_tokens,
        "per_request_token_cap": plan.max_tokens_per_request,
    }
    if plan.cost_per_1k_tokens is not None:
        estimate["worst_case_cost_usd"] = round(worst_tokens / 1000.0 * plan.cost_per_1k_tokens, 6)
    else:
        estimate["worst_case_cost_usd"] = None
        estimate["cost_note"] = (
            "no cost_per_1k_tokens configured: the cost cap is NOT_MEASURED and "
            "cannot be enforced, only the token budget can"
        )
    return estimate


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
    budget_reason: str | None = None

    for case in list(cases)[: plan.max_queries]:
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
            results.append(
                RealCaseResult(
                    case_id=case_id,
                    status=STATUS_ERROR,
                    latency_ms=round((clock() - started) * 1000, 3),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        names, calls = _coerce_tools(outcome.get("tool_calls") or outcome.get("tool_names"))
        in_tok = outcome.get("input_tokens")
        out_tok = outcome.get("output_tokens")
        cost = outcome.get("cost_usd")
        if isinstance(in_tok, int):
            tokens_used += in_tok
        if isinstance(out_tok, int):
            tokens_used += out_tok
        if isinstance(cost, int | float):
            cost_used += float(cost)
        elif (
            plan.cost_per_1k_tokens is not None
            and isinstance(in_tok, int)
            and isinstance(out_tok, int)
        ):
            derived = (in_tok + out_tok) / 1000.0 * plan.cost_per_1k_tokens
            cost_used += derived
            cost = round(derived, 6)

        results.append(
            RealCaseResult(
                case_id=case_id,
                status=STATUS_OK,
                route=outcome.get("route"),
                mode=outcome.get("mode"),
                tool_names=names,
                tool_calls=calls,
                response_nonempty=bool(outcome.get("response_nonempty")),
                latency_ms=round((clock() - started) * 1000, 3),
                input_tokens=in_tok if isinstance(in_tok, int) else None,
                output_tokens=out_tok if isinstance(out_tok, int) else None,
                cost_usd=cost if isinstance(cost, int | float) else None,
            )
        )

    ok = sum(1 for r in results if r.status == STATUS_OK)
    return {
        "plan": plan.redacted(),
        "results": [r.to_dict() for r in results],
        "summary": {
            "attempted": len(results),
            "ok": ok,
            "error": len(results) - ok,
            "tokens_used": tokens_used,
            "cost_used_usd": round(cost_used, 6) if plan.cost_per_1k_tokens else None,
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
    "run_real_provider_lane",
]
