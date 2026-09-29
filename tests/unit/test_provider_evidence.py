from __future__ import annotations

import pytest

from evaluation.evidence_schema import EvidenceSource
from evaluation.provider_adapter import normalize_provider_response, parse_sse_events
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
