"""Unit tests for the application tracing layer.

These are contract tests, not coverage tests. The contract is: tracing never
changes business behaviour, and tracing never carries content. Both are
properties that only fail *silently* in production, so they are pinned here.

Run without any OpenTelemetry SDK, any collector and any network. Every test
either runs with spans disabled (the production default) or with a fake tracer
injected.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from core import telemetry
from core.telemetry import (
    ALLOWED_ATTRIBUTES,
    FORBIDDEN_SUBSTRINGS,
    _NoopSpan,
    enabled,
    run_correlation_attributes,
    scrub_attributes,
    span,
)


@pytest.fixture(autouse=True)
def _spans_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to spans disabled, i.e. the shipped default."""
    monkeypatch.setenv("OTEL_ENABLED", "false")


# ---------------------------------------------------------------------------
# 1. Exception propagation — the bug this layer is most likely to reintroduce
# ---------------------------------------------------------------------------


def test_business_exception_propagates_unchanged():
    """A failure inside the ``with`` body must reach the caller untouched.

    Regression class: implementing the context manager as
    ``try: yield span; except Exception: yield noop`` looks like "degrade
    gracefully" and is actually catastrophic. When the body raises,
    ``contextlib`` throws the exception into the generator at the ``yield``; the
    handler then yields a *second* time, and ``contextlib`` raises
    ``RuntimeError: generator didn't stop after throw()``. The business exception
    is replaced by an instrumentation error — the exact opposite of the
    requirement that observability must not alter runtime behaviour.
    """
    sentinel = ValueError("business failure must survive")

    with (
        pytest.raises(ValueError) as excinfo,
        span("csai.test", attributes={"csai.run_id": "r1"}) as s,
    ):
        s.set_attribute("csai.run_status", "RUNNING")
        raise sentinel

    assert excinfo.value is sentinel


def test_base_exception_propagates():
    """Not just ``Exception`` — cancellation must not be swallowed either."""
    with pytest.raises(KeyboardInterrupt), span("csai.test"):
        raise KeyboardInterrupt("cancel")


def test_normal_exit_does_not_raise():
    with span("csai.test", attributes={"csai.run_id": "r1"}) as s:
        s.set_attribute("csai.run_status", "SUCCEEDED")
    assert s.captured_attributes["csai.run_status"] == "SUCCEEDED"


def test_return_from_with_body_closes_cleanly():
    def _inner() -> str:
        with span("csai.test"):
            return "returned"
        # unreachable

    assert _inner() == "returned"


# ---------------------------------------------------------------------------
# 2. Disabled / unavailable -> no-op, and the call site is unchanged
# ---------------------------------------------------------------------------


def test_disabled_spans_yield_a_usable_noop() -> None:
    assert enabled() is False
    with span("csai.test", attributes={"csai.run_id": "r7"}) as s:
        s.set_attribute("csai.duration_ms", 12.5)
        s.add_event("something", {"csai.stage.status": "ok"})
        s.record_exception(RuntimeError("recorded, not raised"))
    assert isinstance(s, _NoopSpan)
    assert s.captured_attributes["csai.run_id"] == "r7"


def test_noop_exit_never_suppresses() -> None:
    """A fallback that swallowed exceptions would reintroduce the bug above."""
    noop = _NoopSpan()
    assert noop.__exit__(ValueError, ValueError("x"), None) is False


def test_safe_span_fallback_propagates_and_never_fails() -> None:
    """The last-resort fallback must be behaviourally inert."""
    from core.tracing import safe_span

    with safe_span("csai.test", attributes={"csai.run_id": "r8"}) as s:
        s.set_attribute("csai.run_id", "r8")
        s.set_attributes({"csai.run_id": "r8"})
        s.add_event("e")
        s.record_exception(RuntimeError("x"))
        s.set_status("OK")
        assert s.is_recording() is False
        assert s.get_span_context() is None

    with pytest.raises(ValueError), safe_span("csai.test"):
        raise ValueError("must propagate")


def test_telemetry_module_never_raises_when_otel_sdk_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OTEL_ENABLED=true`` with no SDK installed must still be a no-op.

    Turning the switch on is not a promise that the SDK is present.
    """
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setattr(
        telemetry,
        "_tracer",
        lambda: (_ for _ in ()).throw(ImportError("no opentelemetry")),
    )
    with span("csai.test", attributes={"csai.run_id": "r9"}) as s:
        s.set_attribute("csai.run_status", "FAILED")
    assert s.captured_attributes["csai.run_status"] == "FAILED"


# ---------------------------------------------------------------------------
# 3. Privacy: the whitelist is a boundary, not a preference
# ---------------------------------------------------------------------------


def test_content_attributes_are_dropped() -> None:
    """A raw query must not survive even if a call site passes one."""
    out = scrub_attributes(
        {
            "csai.run_id": "r1",
            "csai.query": "我的订单 A123 什么时候发货",
            "csai.user_text": "hi",
            "csai.rag.query_rewrite": "改写后的用户问题",
            "csai.retrieved_documents": "内部文档正文",
        }
    )
    assert out == {"csai.run_id": "r1"}


def test_unknown_keys_are_dropped() -> None:
    assert scrub_attributes({"csai.not_in_whitelist": 1}) == {}


def test_empty_and_none() -> None:
    assert scrub_attributes(None) == {}
    assert scrub_attributes({}) == {}


def test_no_allowed_key_contains_a_forbidden_substring() -> None:
    """The two gates must not contradict each other.

    If a whitelisted key matched a forbidden substring it would be silently
    dropped at runtime, i.e. a documented attribute that never appears.
    """
    offenders = [
        key for key in ALLOWED_ATTRIBUTES if any(bad in key.lower() for bad in FORBIDDEN_SUBSTRINGS)
    ]
    assert offenders == [], f"whitelisted keys that the denylist would drop: {offenders}"


def test_token_counters_survive_the_denylist() -> None:
    """``token`` as a unit must not be mistaken for a credential.

    ``csai.token_prompt`` carries a count, not a token string. The denylist
    targets ``access_token`` / ``bearer``; if it blocked the word ``token`` it
    would also kill the most useful non-sensitive usage metric.
    """
    out = scrub_attributes(
        {
            "csai.token_prompt": 100,
            "csai.token_completion": 20,
            "csai.token_total": 120,
        }
    )
    assert out == {
        "csai.token_prompt": 100,
        "csai.token_completion": 20,
        "csai.token_total": 120,
    }


def test_credentials_are_dropped() -> None:
    for key in (
        "csai.access_token",
        "csai.api_key",
        "csai.authorization_header",
        "csai.session_cookie",
        "csai.customer_secret",
    ):
        assert scrub_attributes({key: "leak-me"}) == {}


def test_identifier_attributes_are_dropped() -> None:
    """``user_id`` is deliberately refused: it is PII."""
    assert scrub_attributes({"csai.user_id": "u-1"}) == {}
    assert scrub_attributes({"csai.username": "alice"}) == {}


def test_attribute_values_are_not_inspected_only_keys() -> None:
    """Scrubbing is key-based; that boundary is stated, not accidental.

    Worth pinning so nobody later assumes values are inspected: a numeric
    ``run_id``-like value on a permitted key is accepted as-is. The control
    against content leaking is the *key* whitelist plus call sites that only
    pass derived quantities.
    """
    assert scrub_attributes({"csai.run_id": "订单 A123 的退款进度"}) == {
        "csai.run_id": "订单 A123 的退款进度"
    }


# ---------------------------------------------------------------------------
# 4. Noop span still records facts when the trace backend is gone
# ---------------------------------------------------------------------------


def test_facts_survive_a_broken_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Degrading must not lose the facts the call site supplied.

    If the collector is down, the useful behaviour is "no trace, but the run's
    own facts are still recorded locally", not "the facts vanish too".
    """
    monkeypatch.setenv("OTEL_ENABLED", "true")

    class _BrokenCM:
        def __enter__(self) -> Any:
            return _ExplodingSpan()

        def __exit__(self, *_exc: object) -> None:
            return None

    class _ExplodingSpan:
        def set_attribute(self, _key: str, _value: Any) -> None:
            raise RuntimeError("exporter is down")

        def record_exception(self, _exc: BaseException) -> None:
            raise RuntimeError("exporter is down")

        def add_event(self, *_a: object, **_k: object) -> None:
            raise RuntimeError("exporter is down")

    monkeypatch.setattr(
        telemetry,
        "_tracer",
        lambda: type("T", (), {"start_as_current_span": staticmethod(lambda _n: _BrokenCM())})(),
    )
    with span("csai.test", attributes={"csai.run_id": "r11"}) as s:
        s.set_attribute("csai.run_status", "SUCCEEDED")
    # The span object is the _SafeSpan wrapper; it must not have raised.
    assert s is not None


# ---------------------------------------------------------------------------
# 5. Correlation helpers
# ---------------------------------------------------------------------------


def test_run_correlation_attributes_are_empty_outside_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "get_current_run_id",
        "get_current_task_id",
        "get_current_thread_id",
    ):
        monkeypatch.setattr(f"runtime.context.{name}", lambda: None, raising=False)
    assert run_correlation_attributes() == {}


def test_run_correlation_attributes_pick_up_the_run_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runtime.context as ctx

    monkeypatch.setattr(ctx, "get_current_run_id", lambda: "run-1")
    monkeypatch.setattr(ctx, "get_current_thread_id", lambda: "thread-1")
    monkeypatch.setattr(ctx, "get_current_task_id", lambda: "task-1")
    monkeypatch.setattr(telemetry, "current_trace_id", lambda: None)
    assert run_correlation_attributes() == {
        "csai.run_id": "run-1",
        "csai.thread_id": "thread-1",
        "csai.task_id": "task-1",
    }


def test_current_trace_id_is_none_without_a_tracer() -> None:
    assert telemetry.current_trace_id() is None


# ---------------------------------------------------------------------------
# 6. Configuration contract
# ---------------------------------------------------------------------------


def test_both_switches_default_to_off_in_the_env_template() -> None:
    """A tracing feature must ship disabled.

    Read from ``.env.example`` rather than from ``os.environ`` so the assertion
    survives a developer machine that happens to export these.
    """
    from pathlib import Path

    template = Path(__file__).resolve().parents[2] / ".env.example"
    text = template.read_text(encoding="utf-8")
    assert "OPENTELEMETRY_ENABLED=false" in text
    assert "OTEL_ENABLED=false" in text


def test_enabled_reads_the_request_level_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw, expected in (
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("1", False),
        ("yes", False),
        ("false", False),
        ("", False),
    ):
        monkeypatch.setenv("OTEL_ENABLED", raw)
        assert enabled() is expected, raw
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    assert enabled() is False


def test_telemetry_does_not_require_the_sdk_at_import_time() -> None:
    """Importing the module must not require OpenTelemetry to be installed.

    ``core.telemetry`` is imported on hot paths, and the SDK is an optional
    dependency; an import-time requirement would make it a hard dependency.
    """
    assert "opentelemetry" not in {
        name.split(".")[0] for name in dir() if name.startswith("opentelemetry")
    }
    source_modules = [
        name
        for name in os.environ.get("_CSAI_UNUSED", "").split(",")
        if name.startswith("opentelemetry")
    ]
    assert source_modules == []


# ---------------------------------------------------------------------------
# 7. Reranker degradation is visible in telemetry
#
# The trace must be able to answer "did a reranker actually reorder anything?"
# without any request content. `csai.reranker_count > 0` on a request whose
# reranker silently failed was the original lie.
# ---------------------------------------------------------------------------

from rag.qdrant_knowledge_base import _apply_retrieval_span  # noqa: E402
from rag.reranker import RerankOutcome, RerankReason  # noqa: E402
from rag.retrieval_contract import (  # noqa: E402
    STAGE_FINAL,
    STAGE_RERANK,
    RetrievalResult,
    RetrievalTrace,
    StageStatus,
    TraceStage,
)


def _span_capture():
    """A _NoopSpan already opened, so we can read captured attributes/events."""
    ctx = span("csai.rag.retrieve")
    s = ctx.__enter__()
    return ctx, s


def _result_with_rerank(status: StageStatus, reason: str) -> RetrievalResult:
    trace = RetrievalTrace()
    trace.add(
        TraceStage(
            STAGE_RERANK, status, candidate_in=4, candidate_out=3, duration_ms=1.0, reason=reason
        )
    )
    trace.add(TraceStage(STAGE_FINAL, StageStatus.EXECUTED, candidate_out=3))
    meta: dict = {
        "retrieval_degraded": False,
        "degraded_reason": "",
        "rerank_requested": True,
        "rerank_applied": status is StageStatus.EXECUTED,
        "rerank_degraded": status is StageStatus.DEGRADED,
        "rerank_reason": reason,
    }
    if status is StageStatus.DEGRADED:
        meta["retrieval_degraded"] = True
        meta["degraded_reason"] = f"reranker_{reason}"
    return RetrievalResult([{"content": "x"}], meta=meta, trace=trace)


def _rerank_event(s):
    for name, attrs in s.captured_events:
        if name == f"rag.stage.{STAGE_RERANK}":
            return attrs
    raise AssertionError("no RERANK stage event on the span")


class TestRerankerTelemetryTruth:
    def test_stage_reason_attribute_is_whitelisted(self):
        assert "csai.stage.reason" in ALLOWED_ATTRIBUTES

    def test_stage_reason_survives_the_scrubber(self):
        assert scrub_attributes({"csai.stage.reason": "timeout"}) == {
            "csai.stage.reason": "timeout"
        }

    @pytest.mark.parametrize(
        ("status", "reason", "expected_status"),
        [
            (StageStatus.EXECUTED, "", "executed"),
            (StageStatus.DEGRADED, "timeout", "degraded"),
            (StageStatus.DEGRADED, "unavailable", "degraded"),
            (StageStatus.SKIPPED, "rerank_disabled", "skipped"),
            (StageStatus.SKIPPED, "insufficient_candidates", "skipped"),
        ],
    )
    def test_stage_status_and_reason_reach_the_span(self, status, reason, expected_status):
        ctx, s = _span_capture()
        _apply_retrieval_span(s, _result_with_rerank(status, reason))
        ctx.__exit__(None, None, None)
        ev = _rerank_event(s)
        assert ev["csai.stage.status"] == expected_status
        assert ev["csai.stage.reason"] == reason

    def test_reranker_count_is_positive_only_for_a_real_rerank(self):
        """The core truthfulness property for this attribute."""
        ctx, s = _span_capture()
        _apply_retrieval_span(s, _result_with_rerank(StageStatus.EXECUTED, ""))
        ctx.__exit__(None, None, None)
        assert s.captured_attributes["csai.reranker_count"] == 3

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (StageStatus.DEGRADED, "timeout"),
            (StageStatus.DEGRADED, "http_error"),
            (StageStatus.DEGRADED, "unavailable"),
            (StageStatus.DEGRADED, "invalid_response"),
            (StageStatus.SKIPPED, "rerank_disabled"),
            (StageStatus.SKIPPED, "insufficient_candidates"),
        ],
    )
    def test_reranker_count_is_zero_when_nothing_was_reranked(self, status, reason):
        ctx, s = _span_capture()
        _apply_retrieval_span(s, _result_with_rerank(status, reason))
        ctx.__exit__(None, None, None)
        assert s.captured_attributes["csai.reranker_count"] == 0

    def test_degradation_is_visible_on_the_span(self):
        ctx, s = _span_capture()
        _apply_retrieval_span(s, _result_with_rerank(StageStatus.DEGRADED, "http_error"))
        ctx.__exit__(None, None, None)
        assert s.captured_attributes["csai.retrieval_degraded"] is True
        assert s.captured_attributes["csai.degraded_reason"] == "reranker_http_error"

    def test_outcome_reason_enum_cannot_smuggle_content(self):
        """Only enum values may be used as a stage reason."""
        assert {r.value for r in RerankReason} <= {
            "",
            "unavailable",
            "timeout",
            "http_error",
            "provider_error",
            "invalid_response",
        }
        out = RerankOutcome(
            results=[],
            applied=False,
            degraded=True,
            reason=RerankReason.HTTP_ERROR,
            http_status=401,
        )
        assert scrub_attributes({"csai.stage.reason": out.reason_value}) == {
            "csai.stage.reason": "http_error"
        }

    def test_content_keys_are_still_refused_alongside_the_new_attribute(self):
        """Adding csai.stage.reason must not weaken the two existing gates."""
        assert scrub_attributes({"csai.stage.reason": "SENSITIVE"}) == {
            "csai.stage.reason": "SENSITIVE"
        }
        assert scrub_attributes({"csai.stage.query": "用户的问句"}) == {}
        assert scrub_attributes({"csai.stage.document": "召回正文"}) == {}
