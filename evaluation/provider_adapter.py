"""Minimal OpenAI-compatible provider adapter for evidence collection.

Only normalized metadata is returned. Prompts, responses, and authorization
headers never enter the evidence artifact.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .evidence_schema import EvidenceSource, Measurement


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _nested(mapping: Mapping[str, Any], *keys: str | int) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _reported(value: Any, unit: str | None = None) -> Measurement:
    number = _number(value)
    return Measurement(number, EvidenceSource.PROVIDER_REPORTED, unit=unit) if number is not None else Measurement.unavailable(
        EvidenceSource.NOT_AVAILABLE
    )


@dataclass(frozen=True)
class NormalizedProviderUsage:
    provider: str
    model: str | None
    request_id: str | None
    input_tokens: Measurement
    output_tokens: Measurement
    cached_tokens: Measurement
    reasoning_tokens: Measurement
    provider_cost: Measurement

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "provider_request_id": self.request_id,
            "provider_input_tokens": self.input_tokens.to_dict(),
            "provider_output_tokens": self.output_tokens.to_dict(),
            "provider_cached_tokens": self.cached_tokens.to_dict(),
            "provider_reasoning_tokens": self.reasoning_tokens.to_dict(),
            "provider_cost": self.provider_cost.to_dict(),
            "usage_source": EvidenceSource.PROVIDER_REPORTED.value,
        }


def normalize_provider_response(
    payload: Mapping[str, Any], *, provider: str, fallback_model: str | None = None
) -> NormalizedProviderUsage:
    """Normalize fields actually used by the project's OpenAI-compatible API."""
    raw_usage = payload.get("usage")
    usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
    cached = _nested(usage, "prompt_tokens_details", "cached_tokens")
    if cached is None:
        cached = _nested(usage, "input_tokens_details", "cached_tokens")
    reasoning = _nested(usage, "completion_tokens_details", "reasoning_tokens")
    if reasoning is None:
        reasoning = usage.get("reasoning_tokens")
    cost = payload.get("cost")
    if cost is None:
        cost = usage.get("cost")
    return NormalizedProviderUsage(
        provider=provider,
        model=payload.get("model") or fallback_model,
        request_id=payload.get("id") or payload.get("request_id"),
        input_tokens=_reported(usage.get("prompt_tokens", usage.get("input_tokens")), "tokens"),
        output_tokens=_reported(usage.get("completion_tokens", usage.get("output_tokens")), "tokens"),
        cached_tokens=_reported(cached, "tokens"),
        reasoning_tokens=_reported(reasoning, "tokens"),
        provider_cost=_reported(cost, "provider_currency"),
    )


def parse_sse_events(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    """Yield JSON SSE data events without retaining prompt/response content."""
    for line in lines:
        if not line or not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if data == "[DONE]":
            return
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


@dataclass(frozen=True)
class ProviderCallResult:
    usage: NormalizedProviderUsage
    ttft_ms: float | None
    e2e_ms: float
    network_ms: float
    attempt_count: int
    retry_count: int
    timeout_count: int
    final_status: str
    response_nonempty: bool
    error_code: str | None = None


class ProviderAdapter:
    def __init__(self, *, api_key: str, base_url: str, model: str, timeout: float, max_attempts: int):
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)

    def stream_chat(self, messages: list[dict[str, str]], *, max_tokens: int, provider: str) -> ProviderCallResult:
        start = time.perf_counter()
        attempts = 0
        retries = 0
        timeouts = 0
        last_error: str | None = None
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            while attempts < self.max_attempts:
                attempts += 1
                try:
                    with client.stream("POST", self._url, json=payload, headers=headers) as response:
                        response.raise_for_status()
                        first_token_at: float | None = None
                        response_nonempty = False
                        final_payload: dict[str, Any] = {}
                        network_start = time.perf_counter()
                        for event in parse_sse_events(response.iter_lines()):
                            if event.get("usage"):
                                final_payload = {**final_payload, "usage": event["usage"]}
                            if event.get("id"):
                                final_payload["id"] = event["id"]
                            if event.get("model"):
                                final_payload["model"] = event["model"]
                            delta = _nested(event, "choices", 0, "delta") if isinstance(_nested(event, "choices"), list) else None
                            if isinstance(delta, Mapping) and delta.get("content"):
                                response_nonempty = True
                                if first_token_at is None:
                                    first_token_at = time.perf_counter()
                        end = time.perf_counter()
                        final_payload.setdefault("id", response.headers.get("x-request-id"))
                        usage = normalize_provider_response(final_payload, provider=provider, fallback_model=self.model)
                        return ProviderCallResult(
                            usage=usage,
                            ttft_ms=(first_token_at - start) * 1000 if first_token_at else None,
                            e2e_ms=(end - start) * 1000,
                            network_ms=(end - network_start) * 1000,
                            attempt_count=attempts,
                            retry_count=retries,
                            timeout_count=timeouts,
                            final_status="SUCCESS",
                            response_nonempty=response_nonempty,
                        )
                except httpx.TimeoutException:
                    timeouts += 1
                    last_error = "TIMEOUT"
                except httpx.HTTPStatusError as exc:
                    last_error = f"HTTP_{exc.response.status_code}"
                except httpx.RequestError as exc:
                    last_error = type(exc).__name__.upper()
                if attempts < self.max_attempts:
                    retries += 1
        elapsed = (time.perf_counter() - start) * 1000
        missing = Measurement.unavailable(EvidenceSource.NOT_AVAILABLE)
        return ProviderCallResult(
            usage=NormalizedProviderUsage(provider, self.model, None, missing, missing, missing, missing, missing),
            ttft_ms=None,
            e2e_ms=elapsed,
            network_ms=elapsed,
            attempt_count=attempts,
            retry_count=retries,
            timeout_count=timeouts,
            final_status="FAILURE",
            response_nonempty=False,
            error_code=last_error,
        )
