"""Controlled provider-staging suite with explicit cost and request guards."""

from __future__ import annotations

import math
import os
import platform
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from .evidence_schema import EvidenceRecord, EvidenceSource, Measurement
from .metrics import summarize_latency
from .provider_adapter import ProviderAdapter, ProviderCallResult
from .runner import _git_sha, _unverified_record
from .workloads import local_workloads


def _missing(note: str) -> Measurement:
    return Measurement.unavailable(EvidenceSource.NOT_AVAILABLE, note)


def _estimate_input_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _placeholder(value: str) -> bool:
    lowered = value.lower()
    return not value or lowered.startswith(("your-", "sk-placeholder", "change-me", "replace-"))


def provider_preflight(
    *, max_requests: int, max_input_tokens: int, max_output_tokens: int, estimated_cost_cap: float
) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    return {
        "credential_present": not _placeholder(api_key),
        "model_configured": bool(os.getenv("OPENAI_MODEL", "Qwen/Qwen3-8B")),
        "base_url_configured": bool(os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")),
        "request_cap": max_requests,
        "input_token_cap": max_input_tokens,
        "output_token_cap_per_request": max_output_tokens,
        "estimated_cost_cap": estimated_cost_cap,
        "artifact_external_calls": False,
    }


def _aggregate_measurements(
    results: list[ProviderCallResult], attribute: str, unit: str
) -> Measurement:
    values = [getattr(result.usage, attribute).value for result in results]
    if not values or any(value is None for value in values):
        return _missing(f"provider did not report {attribute} for every measured request")
    return Measurement(sum(values), EvidenceSource.PROVIDER_REPORTED, unit=unit)


def run_provider_staging(
    *,
    repeat: int = 2,
    warmup: int = 1,
    max_requests: int = 20,
    max_input_tokens: int = 2_000,
    max_output_tokens: int = 256,
    estimated_cost_cap: float = 1.0,
    estimated_input_cost_per_1k: float | None = None,
    estimated_output_cost_per_1k: float | None = None,
) -> EvidenceRecord | dict[str, Any]:
    cases = local_workloads()[:5]
    total_requests = len(cases) * (repeat + warmup)
    if repeat <= 0 or warmup < 0:
        raise ValueError("repeat must be positive and warmup cannot be negative")
    if total_requests > max_requests:
        raise ValueError("request cap is below the planned warmup plus measured requests")
    if (
        max_requests <= 0
        or max_input_tokens <= 0
        or max_output_tokens <= 0
        or estimated_cost_cap <= 0
    ):
        raise ValueError("all provider safety caps must be positive")
    preflight = provider_preflight(
        max_requests=max_requests,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        estimated_cost_cap=estimated_cost_cap,
    )
    if os.getenv("EVAL_REAL_PROVIDER") != "1":
        record = _unverified_record("provider-staging")
        record.update(
            {
                "provider": os.getenv("LLM_PROVIDER", "siliconflow"),
                "model": os.getenv("OPENAI_MODEL", "Qwen/Qwen3-8B"),
                "workload_id": "provider-staging-v1",
                "scenario": "controlled_provider_staging",
                "preflight": preflight,
                "external_calls": 0,
            }
        )
        return record
    if not preflight["credential_present"]:
        raise RuntimeError("REAL_PROVIDER_RUN=BLOCKED_BY_CREDENTIAL")
    max_attempts = int(os.getenv("EVAL_PROVIDER_MAX_ATTEMPTS", "2"))
    if max_attempts <= 0:
        raise ValueError("EVAL_PROVIDER_MAX_ATTEMPTS must be positive")
    planned_attempts = total_requests * max_attempts
    if planned_attempts > max_requests:
        raise RuntimeError(
            "request cap includes worst-case provider retry attempts; no request made"
        )
    if estimated_input_cost_per_1k is None or estimated_output_cost_per_1k is None:
        raise RuntimeError(
            "estimated cost rates are required for the cost cap; no price lookup is performed"
        )
    # Worst case every planned request is retried up to max_attempts: warmup
    # and retried attempts are billable too. Guarding only the measured repeats
    # undercounted real spend several-fold.
    planned_input = (
        sum(_estimate_input_tokens(case.query) for case in cases) * (repeat + warmup) * max_attempts
    )
    if planned_input > max_input_tokens:
        raise RuntimeError("planned estimated input-token cap would be exceeded; no request made")
    planned_estimated_cost = (
        sum(
            (_estimate_input_tokens(case.query) / 1000) * estimated_input_cost_per_1k
            + (max_output_tokens / 1000) * estimated_output_cost_per_1k
            for case in cases
        )
        * (repeat + warmup)
        * max_attempts
    )
    if planned_estimated_cost > estimated_cost_cap:
        raise RuntimeError("planned estimated cost cap would be exceeded; no request made")

    provider = os.getenv("LLM_PROVIDER", "siliconflow")
    model = os.getenv("OPENAI_MODEL", "Qwen/Qwen3-8B")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
    adapter = ProviderAdapter(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=base_url,
        model=model,
        timeout=float(os.getenv("EVAL_PROVIDER_TIMEOUT", "30")),
        max_attempts=max_attempts,
    )
    results: list[ProviderCallResult] = []
    measured_results: list[ProviderCallResult] = []
    input_budget = 0
    output_budget = 0
    warmup_count = len(cases) * warmup
    for index in range(total_requests):
        case = cases[index % len(cases)]
        prompt = case.query
        input_budget += _estimate_input_tokens(prompt)
        if input_budget > max_input_tokens:
            raise RuntimeError(
                "estimated input-token cap would be exceeded; no further request made"
            )
        result = adapter.stream_chat(
            [{"role": "user", "content": prompt}], max_tokens=max_output_tokens, provider=provider
        )
        results.append(result)
        if result.usage.output_tokens.value is not None:
            output_budget += int(result.usage.output_tokens.value)
        if output_budget > max_requests * max_output_tokens:
            raise RuntimeError("provider output-token safety budget exceeded")
        is_warmup = index < warmup_count
        if not is_warmup:
            measured_results.append(result)
        if result.error_category in {"AUTH_FAILED", "FORBIDDEN"}:
            # A terminating auth failure is accounted even when it happened
            # during warmup; otherwise measured_results is empty and the run
            # would look like a zero-request PASS while the breaker fired.
            if is_warmup:
                measured_results.append(result)
            break
    authentication_blocked = any(
        result.error_category in {"AUTH_FAILED", "FORBIDDEN"} for result in results
    )
    e2e = [result.e2e_ms for result in measured_results]
    ttft = [result.ttft_ms for result in measured_results if result.ttft_ms is not None]
    latency = summarize_latency(e2e, warmup_count=0, source=EvidenceSource.APPLICATION_MEASURED)
    ttft_summary = summarize_latency(
        ttft, warmup_count=0, source=EvidenceSource.APPLICATION_MEASURED
    )
    successes = sum(
        result.final_status == "SUCCESS" and result.response_nonempty for result in measured_results
    )
    failures = len(measured_results) - successes
    if authentication_blocked:
        final_status = "BLOCKED_BY_AUTHENTICATION"
    elif successes == 0:
        # Zero successful requests is never "partial success".
        final_status = "FAILED"
    elif failures == 0:
        final_status = "PASS"
    else:
        final_status = "PARTIAL"
    costs = _aggregate_measurements(measured_results, "provider_cost", "provider_currency")
    estimated_cost = Measurement(
        planned_estimated_cost,
        EvidenceSource.ESTIMATED,
        unit="configured_currency",
    )
    estimated_cost_value = estimated_cost.value
    if not isinstance(estimated_cost_value, int | float):
        raise RuntimeError("estimated cost could not be calculated safely")
    if estimated_cost_value > estimated_cost_cap:
        raise RuntimeError(
            "estimated cost cap would be exceeded; no artifact marked as production evidence"
        )
    return EvidenceRecord(
        run_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc).isoformat(),
        git_sha=_git_sha(),
        environment="CONTROLLED_STAGING",
        provider=provider,
        model=model,
        workload_id="provider-staging-v1",
        scenario="controlled_provider_staging",
        request_count=len(measured_results),
        success_count=successes,
        failure_count=failures,
        latency=latency,
        component_latency={"e2e": latency, "ttft": ttft_summary},
        estimated_tokens=_missing("estimated token totals are not evidence fields"),
        input_tokens=_aggregate_measurements(measured_results, "input_tokens", "tokens"),
        output_tokens=_aggregate_measurements(measured_results, "output_tokens", "tokens"),
        cached_tokens=_aggregate_measurements(measured_results, "cached_tokens", "tokens"),
        provider_cost=costs,
        estimated_cost=estimated_cost,
        provider_request_id=measured_results[-1].usage.request_id if measured_results else None,
        attempt_count=sum(result.attempt_count for result in measured_results),
        retry_count=sum(result.retry_count for result in measured_results),
        timeout_count=sum(result.timeout_count for result in measured_results),
        provider_error_code=next(
            (result.error_code for result in (*measured_results, *results) if result.error_code),
            None,
        ),
        final_status=final_status,
        metadata={
            "python": sys.version.split()[0],
            "os": platform.platform(),
            "warmup_count": warmup_count,
            "measured_count": len(measured_results),
            "planned_attempts": planned_attempts,
            "planned_worst_case_input_tokens": planned_input,
            "planned_worst_case_estimated_cost": planned_estimated_cost,
            "estimated_cost_semantics": "worst-case plan over cases x (warmup+repeat) x max_attempts",
            "external_calls": sum(result.attempt_count for result in results),
            "authentication_circuit_breaker": authentication_blocked,
            "preflight": preflight,
            "artifact_privacy": "metrics_only_no_prompt_or_response",
        },
        notes=[
            "CONTROLLED_STAGING: this is not production latency or production task-success evidence."
        ],
        samples=[
            {
                "scenario_id": cases[index % len(cases)].workload_id,
                "status": result.final_status,
                "ttft_ms": result.ttft_ms,
                "e2e_ms": result.e2e_ms,
                "network_ms": result.network_ms,
                "attempt_count": result.attempt_count,
                "retry_count": result.retry_count,
                "timeout_count": result.timeout_count,
                "provider_error_code": result.error_code,
                "validation": "non_empty_response",
                "response_nonempty": result.response_nonempty,
                "provider_request_id": result.usage.request_id,
                "usage": result.usage.to_dict(),
            }
            for index, result in enumerate(measured_results)
        ],
    )
