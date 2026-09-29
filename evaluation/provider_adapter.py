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

NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 422}


class ProviderAuthenticationError(Exception):
    """Raised when a provider credential cannot authenticate a request."""


class ProviderCredentialFormatError(ValueError):
    """Raised when a credential contains an unsupported wrapper or whitespace."""


def normalize_api_key(raw_key: str | None) -> str:
    """Accept a plain key after whitespace trimming; never add/remove wrappers."""
    key = (raw_key or "").strip()
    if not key:
        raise ProviderCredentialFormatError("credential is missing")
    if key.lower().startswith("bearer "):
        raise ProviderCredentialFormatError("credential must not include a Bearer prefix")
    if "\n" in key or "\r" in key or "\"" in key or "'" in key:
        raise ProviderCredentialFormatError("credential contains unsupported quoting or newline")
    return key


def _trace_id(headers: Mapping[str, str]) -> str | None:
    return headers.get("x-siliconcloud-trace-id") or headers.get("x-request-id")


def _status_category(status_code: int) -> tuple[str, bool]:
    if status_code == 200:
        return "AUTHENTICATED", False
    if status_code == 401:
        return "AUTH_FAILED", False
    if status_code == 403:
        return "FORBIDDEN", False
    if status_code == 404:
        return "INVALID_ENDPOINT_OR_MODEL", False
    if status_code == 408 or status_code == 429 or status_code >= 500:
        return "TRANSIENT_PROVIDER_FAILURE", True
    return "HTTP_ERROR", False


@dataclass(frozen=True)
class AuthProbeResult:
    status_code: int | None
    authenticated: bool
    error_category: str
    retryable: bool
    attempt_count: int
    trace_id: str | None
    model_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChatProbeResult:
    status_code: int | None
    category: str
    retryable: bool
    attempt_count: int
    trace_id: str | None
    usage: NormalizedProviderUsage | None
    model: str | None
    response_nonempty: bool


def _probe_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def probe_provider_auth(
    *, api_key: str | None, base_url: str, timeout: float = 15.0
) -> AuthProbeResult:
    """Perform exactly one safe GET /models request and discard the raw body."""
    try:
        normalized_key = normalize_api_key(api_key)
    except ProviderCredentialFormatError as exc:
        category = "CREDENTIAL_MISSING" if "missing" in str(exc) else "CREDENTIAL_FORMAT_INVALID"
        return AuthProbeResult(None, False, category, False, 0, None)

    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/models",
                params={"sub_type": "chat"},
                headers=_probe_headers(normalized_key),
            )
        category, retryable = _status_category(response.status_code)
        model_ids: tuple[str, ...] = ()
        if response.status_code == 200:
            try:
                body = response.json()
                data = body.get("data", []) if isinstance(body, Mapping) else []
                model_ids = tuple(
                    item["id"] for item in data if isinstance(item, Mapping) and isinstance(item.get("id"), str)
                )
            except (ValueError, TypeError):
                category, retryable = "INVALID_PROVIDER_RESPONSE", False
        return AuthProbeResult(
            response.status_code,
            response.status_code == 200 and category == "AUTHENTICATED",
            category,
            retryable,
            1,
            _trace_id(response.headers),
            model_ids,
        )
    except httpx.TimeoutException:
        return AuthProbeResult(None, False, "TIMEOUT", True, 1, None)
    except httpx.RequestError as exc:
        return AuthProbeResult(None, False, type(exc).__name__.upper(), True, 1, None)


def probe_chat_completion(
    *, api_key: str, base_url: str, model: str, timeout: float = 15.0
) -> ChatProbeResult:
    """Perform one non-streaming, low-budget request without retrying."""
    normalized_key = normalize_api_key(api_key)
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "stream": False,
                    "max_tokens": 8,
                    "temperature": 0,
                },
                headers={**_probe_headers(normalized_key), "Content-Type": "application/json"},
            )
        category, retryable = _status_category(response.status_code)
        usage: NormalizedProviderUsage | None = None
        response_nonempty = False
        response_model: str | None = None
        if response.status_code == 200:
            body = response.json()
            if not isinstance(body, Mapping):
                return ChatProbeResult(200, "INVALID_PROVIDER_RESPONSE", False, 1, _trace_id(response.headers), None, None, False)
            usage = normalize_provider_response(body, provider="siliconflow", fallback_model=model)
            response_model = usage.model
            choices = body.get("choices")
            response_nonempty = bool(
                isinstance(choices, list)
                and choices
                and isinstance(choices[0], Mapping)
                and isinstance(choices[0].get("message"), Mapping)
                and choices[0]["message"].get("content")
            )
            category = "CHAT_API_OK" if response_nonempty else "EMPTY_PROVIDER_RESPONSE"
        return ChatProbeResult(
            response.status_code,
            category,
            retryable,
            1,
            _trace_id(response.headers),
            usage,
            response_model,
            response_nonempty,
        )
    except httpx.TimeoutException:
        return ChatProbeResult(None, "TIMEOUT", True, 1, None, None, None, False)
    except httpx.RequestError as exc:
        return ChatProbeResult(None, type(exc).__name__.upper(), True, 1, None, None, None, False)


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
    error_category: str | None = None
    http_status: int | None = None


class ProviderAdapter:
    def __init__(self, *, api_key: str, base_url: str, model: str, timeout: float, max_attempts: int):
        self._api_key = normalize_api_key(api_key)
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
                    status_code = exc.response.status_code
                    last_error = f"HTTP_{status_code}"
                    if status_code in NON_RETRYABLE_STATUS_CODES:
                        return ProviderCallResult(
                            usage=NormalizedProviderUsage(
                                provider,
                                self.model,
                                response.headers.get("x-request-id"),
                                *[Measurement.unavailable(EvidenceSource.NOT_AVAILABLE)] * 5,
                            ),
                            ttft_ms=None,
                            e2e_ms=(time.perf_counter() - start) * 1000,
                            network_ms=(time.perf_counter() - start) * 1000,
                            attempt_count=attempts,
                            retry_count=retries,
                            timeout_count=timeouts,
                            final_status="FAILURE",
                            response_nonempty=False,
                            error_code=last_error,
                            error_category=_status_category(status_code)[0],
                            http_status=status_code,
                        )
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
            error_category=(
                _status_category(int(last_error.split("_")[1]))[0]
                if last_error and last_error.startswith("HTTP_")
                else last_error
            ),
            http_status=(int(last_error.split("_")[1]) if last_error and last_error.startswith("HTTP_") else None),
        )
