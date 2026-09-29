from __future__ import annotations

import httpx
import pytest

from evaluation.evidence_schema import EvidenceSource, Measurement
from evaluation.provider_adapter import (
    NormalizedProviderUsage,
    ProviderAdapter,
    ProviderCallResult,
    normalize_api_key,
    normalize_provider_response,
    parse_sse_events,
    probe_provider_auth,
)
from evaluation.provider_runner import run_provider_staging


def test_normalizes_actual_openai_compatible_usage_fields() -> None:
    usage = normalize_provider_response(
        {
            "id": "req-safe-test",
            "model": "staging-model",
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        },
        provider="test-provider",
    )
    assert usage.input_tokens.value == 11
    assert usage.output_tokens.value == 7
    assert usage.cached_tokens.value == 3
    assert usage.reasoning_tokens.value == 2
    assert usage.provider_cost.source is EvidenceSource.NOT_AVAILABLE
    assert usage.request_id == "req-safe-test"


def test_missing_and_partial_usage_never_becomes_zero() -> None:
    usage = normalize_provider_response({"usage": {"prompt_tokens": 4}}, provider="test-provider")
    assert usage.input_tokens.value == 4
    assert usage.output_tokens.source is EvidenceSource.NOT_AVAILABLE
    assert usage.cached_tokens.source is EvidenceSource.NOT_AVAILABLE


def test_stream_parser_discards_non_data_and_done() -> None:
    events = list(parse_sse_events(["", "comment", 'data: {"id":"x"}', "data: [DONE]", 'data: {"id":"ignored"}']))
    assert events == [{"id": "x"}]


def test_provider_staging_is_disabled_without_explicit_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_REAL_PROVIDER", raising=False)
    result = run_provider_staging(repeat=1, warmup=1)
    assert result["external_calls"] == 0
    assert result["environment"] == "NOT_VERIFIED"


def test_cost_cap_fails_before_any_provider_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_REAL_PROVIDER", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-sent")
    with pytest.raises(RuntimeError, match="estimated cost cap"):
        run_provider_staging(
            repeat=1,
            warmup=0,
            estimated_cost_cap=0.000001,
            estimated_input_cost_per_1k=1.0,
            estimated_output_cost_per_1k=1.0,
        )


def test_request_cap_includes_worst_case_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_REAL_PROVIDER", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-sent")
    monkeypatch.setenv("EVAL_PROVIDER_MAX_ATTEMPTS", "2")
    with pytest.raises(RuntimeError, match="retry attempts"):
        run_provider_staging(
            repeat=2,
            warmup=1,
            max_requests=20,
            estimated_cost_cap=1.0,
            estimated_input_cost_per_1k=0.0015,
            estimated_output_cost_per_1k=0.006,
        )


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = {"x-siliconcloud-trace-id": "trace-safe-test"}
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "provider error",
                request=httpx.Request("POST", "https://api.siliconflow.cn/v1/chat/completions"),
                response=httpx.Response(self.status_code),
            )

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_lines(self) -> list[str]:
        return []


class _FakeClient:
    response: _FakeResponse

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, *_args: object, **_kwargs: object) -> _FakeResponse:
        return self.response

    def stream(self, *_args: object, **_kwargs: object) -> _FakeResponse:
        return self.response


@pytest.mark.parametrize(
    ("status_code", "category", "retryable"),
    [(401, "AUTH_FAILED", False), (403, "FORBIDDEN", False), (429, "TRANSIENT_PROVIDER_FAILURE", True), (503, "TRANSIENT_PROVIDER_FAILURE", True)],
)
def test_auth_probe_classifies_status_without_retry(
    monkeypatch: pytest.MonkeyPatch, status_code: int, category: str, retryable: bool
) -> None:
    monkeypatch.setattr(
        "evaluation.provider_adapter.httpx.Client",
        lambda **_kwargs: _FakeClient(_FakeResponse(status_code)),
    )
    result = probe_provider_auth(api_key="test-only-key", base_url="https://api.siliconflow.cn/v1")
    assert result.status_code == status_code
    assert result.error_category == category
    assert result.retryable is retryable
    assert result.attempt_count == 1


def test_auth_probe_200_discovers_chat_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "evaluation.provider_adapter.httpx.Client",
        lambda **_kwargs: _FakeClient(_FakeResponse(200, {"data": [{"id": "Qwen/test"}]})),
    )
    result = probe_provider_auth(api_key="test-only-key", base_url="https://api.siliconflow.cn/v1")
    assert result.authenticated is True
    assert result.model_ids == ("Qwen/test",)


def test_credential_normalization_rejects_bearer_wrapper() -> None:
    with pytest.raises(ValueError, match="Bearer"):
        normalize_api_key("Bearer test-only-key")


def test_missing_credential_blocks_without_network() -> None:
    result = probe_provider_auth(api_key=None, base_url="https://api.siliconflow.cn/v1")
    assert result.error_category == "CREDENTIAL_MISSING"
    assert result.attempt_count == 0


def test_streaming_401_is_single_non_retryable_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "evaluation.provider_adapter.httpx.Client",
        lambda **_kwargs: _FakeClient(_FakeResponse(401)),
    )
    result = ProviderAdapter(
        api_key="test-only-key",
        base_url="https://api.siliconflow.cn/v1",
        model="Qwen/test",
        timeout=1,
        max_attempts=3,
    ).stream_chat([{"role": "user", "content": "test"}], max_tokens=8, provider="test")
    assert result.attempt_count == 1
    assert result.retry_count == 0
    assert result.error_category == "AUTH_FAILED"
    assert result.http_status == 401


def test_provider_suite_authentication_circuit_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = EvidenceSource.NOT_AVAILABLE
    usage = NormalizedProviderUsage(
        provider="test",
        model="Qwen/test",
        request_id=None,
        input_tokens=Measurement.unavailable(missing),
        output_tokens=Measurement.unavailable(missing),
        cached_tokens=Measurement.unavailable(missing),
        reasoning_tokens=Measurement.unavailable(missing),
        provider_cost=Measurement.unavailable(missing),
    )
    calls = 0

    class _FailFastAdapter:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def stream_chat(self, *_args: object, **_kwargs: object) -> ProviderCallResult:
            nonlocal calls
            calls += 1
            return ProviderCallResult(
                usage=usage,
                ttft_ms=None,
                e2e_ms=1.0,
                network_ms=1.0,
                attempt_count=1,
                retry_count=0,
                timeout_count=0,
                final_status="FAILURE",
                response_nonempty=False,
                error_code="HTTP_401",
                error_category="AUTH_FAILED",
                http_status=401,
            )

    monkeypatch.setenv("EVAL_REAL_PROVIDER", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-sent")
    monkeypatch.setenv("EVAL_PROVIDER_MAX_ATTEMPTS", "1")
    monkeypatch.setattr("evaluation.provider_runner.ProviderAdapter", _FailFastAdapter)
    result = run_provider_staging(
        repeat=1,
        warmup=0,
        max_requests=5,
        estimated_cost_cap=1.0,
        estimated_input_cost_per_1k=0.0015,
        estimated_output_cost_per_1k=0.006,
    )
    assert calls == 1
    assert result.request_count == 1
    assert result.metadata["authentication_circuit_breaker"] is True
