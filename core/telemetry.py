"""Application-level tracing spans (lightweight; **never** a runtime single point of failure).

Positioning
-----------
``core/tracing.py`` is **infrastructure** only: it installs a TracerProvider and
auto-instruments FastAPI / httpx / SQLAlchemy. That tells you an HTTP request
happened. It cannot answer the questions an interviewer actually asks about this
system: which agent ran, how many documents were retrieved, how many survived
reranking, which tool, where a human approval parked the run, and what the
``error_type`` was on failure.

This module adds the **application semantics**, under three hard constraints:

1. **Fully degradable.** With ``OTEL_ENABLED=false`` — or the SDK missing, or the
   exporter unreachable — :func:`span` degrades to a no-op context manager and
   the call site does not change by a single line.
2. **Never changes business semantics.** It does not participate in AgentRun
   state transitions, retry accounting, approval, or idempotency keys. Every
   OpenTelemetry failure is swallowed. In particular, **an exception raised by the
   caller's ``with`` body always propagates unchanged**; see :func:`span` for why
   that needs care.
3. **Records no sensitive content.** Only whitelisted attributes are written. Raw
   prompts, user text, retrieved documents, tool arguments, PII and credentials
   never reach a trace — see :data:`ALLOWED_ATTRIBUTES` and
   :data:`FORBIDDEN_SUBSTRINGS`. This is a boundary, not a style preference:
   traces flow to collectors and third-party backends, so anything in them has
   left the trust boundary.

HITL and traces: what is and is not promised
-------------------------------------------
A run can sit in ``WAITING_APPROVAL`` for a long time (default TTL 3600s), and the
approval decision arrives in a **different HTTP request**. OpenTelemetry context
does not survive across processes or across a long pause, so this module makes
**no** attempt to present the pre-pause and post-resume segments as one span.
Instead:

- each segment is its own trace (``csai.agent.execute`` /
  ``csai.agent.execute.resume``);
- they are correlated explicitly through the ``run_id`` and ``approval_id``
  attributes.

So the promise is *"``run_id`` correlates the two segments"*, **not** *"a single
span spans an arbitrarily long pause"*.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any, Literal

from core.logger import get_logger

logger = get_logger("core.telemetry")

#: Attribute keys allowed on a span. **Deliberately short** — every extra key is
#: extra leak surface. Adding one is a reviewable event, and
#: ``tests/unit/test_telemetry.py`` fails if a key here is not exercised or if a
#: forbidden substring appears in it.
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        # correlation
        "csai.run_id",
        "csai.thread_id",
        "csai.task_id",
        "csai.trace_id",
        "csai.approval_id",
        "csai.approval_state",
        "csai.resumed",
        # routing / agents
        "csai.intent",
        "csai.agent_name",
        "csai.collaboration_mode",
        "csai.agents_used",
        # models
        "csai.model_name",
        "csai.embedding_model",
        "csai.reranker_model",
        "csai.token_prompt",
        "csai.token_completion",
        "csai.token_total",
        # RAG
        "csai.rag.scene",
        "csai.retrieval_top_k",
        "csai.retrieval_result_count",
        "csai.reranker_count",
        "csai.retrieval_degraded",
        "csai.degraded_reason",
        # per-RAG-stage event attributes: counts / status / duration, no text
        "csai.stage.status",
        "csai.stage.candidate_in",
        "csai.stage.candidate_out",
        "csai.stage.duration_ms",
        # Bounded, low-cardinality stage cause (e.g. "timeout", "rerank_disabled").
        # Only ever a value from a reason enum — never an exception message, a
        # query, a document or a provider response.
        "csai.stage.reason",
        # tools / HITL / side effects
        "csai.tool_name",
        "csai.tool_side_effect",
        "csai.side_effect_deduplicated",
        "csai.risk_level",
        # run outcome
        "csai.run_status",
        "csai.retry_attempt",
        "csai.error_type",
        "csai.error_code",
        "csai.duration_ms",
    }
)

#: Attribute keys explicitly refused (substring match, case-insensitive). A second
#: gate behind the whitelist.
#:
#: Only names whose **value** would be sensitive are refused. ``token`` is a
#: legitimate unit (``csai.token_prompt`` carries a number, never a token string),
#: so the refused names are the ones that actually carry credentials or content —
#: ``access_token`` / ``bearer`` — not the word ``token`` itself. Refusing that
#: would also kill the most useful, entirely non-sensitive usage metric.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "prompt_text",
    "system_prompt",
    "raw_prompt",
    "query",
    "question",
    "message",
    "raw_text",
    "document",
    "chunk_text",
    "answer_text",
    "email",
    "phone",
    "mobile",
    "id_card",
    "access_token",
    "refresh_token",
    "session_token",
    "bearer",
    "api_key",
    "apikey",
    "secret",
    "password",
    "authorization",
    "cookie",
    "credential",
    "user_id",
    "username",
    "proposal",
    "raw_input",
    "raw_output",
)


def enabled() -> bool:
    """Is application-level span production enabled?

    Kept **separate** from ``core/tracing.py``'s ``OPENTELEMETRY_ENABLED`` on
    purpose: the latter decides whether a provider and an exporter (network
    overhead) are installed, this one decides whether spans are produced (per
    request overhead). Splitting them allows exercising span logic with no
    collector installed, and running with a collector while turning spans off to
    cut cost.
    """
    return os.getenv("OTEL_ENABLED", "false").strip().lower() == "true"


def scrub_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """Filter attributes through whitelist ∩ not-sensitive.

    Two gates rather than one: the whitelist blocks keys nobody thought about,
    the denylist blocks keys that look harmless but actually carry content (for
    example a query rewrite under a key like ``csai.rag.query_rewrite``).
    """
    if not attributes:
        return {}
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if key not in ALLOWED_ATTRIBUTES:
            logger.debug("trace 属性被白名单丢弃: %s", key)
            continue
        lowered = key.lower()
        if any(bad in lowered for bad in FORBIDDEN_SUBSTRINGS):
            # Nothing in the whitelist should match these; a match means someone
            # added a key they should not have.
            logger.warning("trace 属性命中敏感词，已丢弃: %s", key)
            continue
        out[key] = value
    return out


class _NoopSpan:
    """No-op span used when spans are disabled or unavailable.

    Still supports ``set_attribute`` / ``record_exception`` / ``set_status`` so
    call sites need no ``if tracing:`` branch — that is what "degrading does not
    change the call site" means in practice.
    """

    __slots__ = ("_attrs", "_events")

    def __init__(self) -> None:
        self._attrs: dict[str, Any] = {}
        self._events: list[tuple[str, dict[str, Any]]] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self._attrs[key] = value

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        self._attrs.update(scrub_attributes(attributes))

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self._events.append((name, scrub_attributes(attributes)))

    def record_exception(self, exc: BaseException) -> None:
        self.add_event("exception", {"exception.type": type(exc).__name__})

    def set_status(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def is_recording(self) -> bool:
        return False

    def get_span_context(self) -> None:
        return None

    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, *_exc: Any) -> Literal[False]:
        # ``Literal[False]``, not ``bool``: mypy must be able to see that this can
        # never suppress an exception. A no-op span that swallowed exceptions
        # would silently change business behaviour.
        return False

    # -- test observability ----------------------------------------------
    @property
    def captured_attributes(self) -> dict[str, Any]:
        return dict(self._attrs)

    @property
    def captured_events(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self._events)


@contextlib.contextmanager
def _null_span() -> Iterator[_NoopSpan]:
    yield _NoopSpan()


class _SafeSpan:
    """Wraps a real span: swallows OpenTelemetry errors, degrades to no-op records."""

    __slots__ = ("_fallback", "_real")

    def __init__(self, real: Any):
        self._real = real
        self._fallback: _NoopSpan | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        try:
            self._real.set_attribute(key, value)
        except Exception as exc:  # noqa: BLE001
            logger.debug("set_attribute 失败（忽略）: %s", exc)

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        for key, value in scrub_attributes(attributes).items():
            self.set_attribute(key, value)

    def add_event(self, event_name: str, attributes: dict[str, Any] | None = None) -> None:
        try:
            self._real.add_event(event_name, scrub_attributes(attributes))
        except Exception as exc:  # noqa: BLE001
            logger.debug("add_event 失败（忽略）: %s", exc)

    def record_exception(self, exc: BaseException) -> None:
        try:
            self._real.record_exception(exc)
        except Exception as inner:  # noqa: BLE001
            logger.debug("record_exception 失败（忽略）: %s", inner)

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        try:
            self._real.set_status(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("set_status 失败（忽略）: %s", exc)

    def is_recording(self) -> bool:
        try:
            return bool(self._real.is_recording())
        except Exception:  # noqa: BLE001
            return False

    def get_span_context(self) -> Any:
        try:
            return self._real.get_span_context()
        except Exception:  # noqa: BLE001
            return None

    @property
    def real(self) -> Any:
        return self._real


@contextlib.contextmanager
def span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Produce an application span; degrade to a no-op when unavailable.

    Usage::

        with span("csai.agent.execute", attributes={"csai.run_id": run_id}) as s:
            s.set_attribute("csai.run_status", "SUCCEEDED")

    Exception semantics — the subtle part
    -------------------------------------
    The caller's exception must reach the caller. A naive implementation wraps
    ``yield`` in ``try/except Exception``, which is wrong in a way that is easy to
    miss: when the ``with`` body raises, ``contextlib`` throws that exception
    into the generator at the ``yield``; catching it and yielding a *second* time
    makes ``contextlib`` raise ``RuntimeError: generator didn't stop after
    throw()``. The business exception is replaced by a confusing error from the
    instrumentation layer — which is precisely the failure mode this module
    exists to prevent.

    So only span *setup* is guarded. The body runs in its own ``try`` whose only
    job is to tell the span how it ended and then re-raise unchanged.
    """
    safe_attrs = scrub_attributes(attributes)

    # ``_tracer()`` is itself defensive, but the guard covers it anyway: the
    # invariant "span() cannot raise before the body runs" should not depend on
    # how ``_tracer`` is implemented or monkeypatched.
    try:
        tracer = _tracer()
    except Exception as exc:  # noqa: BLE001 - observability must not break runtime
        logger.warning("tracer 解析失败，降级为 no-op: %s %s", type(exc).__name__, exc)
        tracer = None

    # ``opened`` is None **or** a (context manager, live span) pair. Keeping them
    # in one optional tuple is what lets a type checker prove that reaching the
    # body implies both are usable, instead of two variables that must be
    # narrowed independently.
    opened: tuple[contextlib.AbstractContextManager[Any], Any] | None = None
    if tracer is not None:
        try:
            candidate = tracer.start_as_current_span(name)
            opened = (candidate, candidate.__enter__())
        except Exception as exc:  # noqa: BLE001 - observability must not break runtime
            logger.warning(
                "trace span 创建失败，降级为 no-op: %s %s", type(exc).__name__, exc
            )
            opened = None

    if opened is None:
        # Degraded path still keeps the caller's facts: if the exporter flickers,
        # the facts of that call must not be lost just because the trace is gone.
        with _null_span() as noop:
            noop.set_attributes(safe_attrs)
            yield noop
        return

    active_ctx, real_span = opened
    wrapped = _SafeSpan(real_span)
    wrapped.set_attributes(safe_attrs)
    try:
        yield wrapped
    except BaseException as exc:
        wrapped.record_exception(exc)
        with contextlib.suppress(Exception):
            active_ctx.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        with contextlib.suppress(Exception):
            active_ctx.__exit__(None, None, None)


def _tracer() -> Any | None:
    """Return the initialised tracer, or ``None`` when disabled / unavailable."""
    if not enabled():
        return None
    try:
        from core.tracing import get_tracer

        return get_tracer("csai.app")
    except Exception as exc:  # noqa: BLE001
        logger.debug("tracer 获取失败（忽略）: %s", exc)
        return None


def current_trace_id() -> str | None:
    """Current trace id (hex), or ``None`` outside a recording span.

    Used to write ``trace_id`` and ``run_id`` next to each other, which is the
    cheapest possible way to correlate a run with its trace without requiring a
    trace backend: the same two ids also land in logs and the run event stream.
    """
    tracer = _tracer()
    if tracer is None:
        return None
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx is None or not getattr(ctx, "is_valid", False):
            return None
        return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001
        return None


def run_correlation_attributes() -> dict[str, Any]:
    """Correlation attributes from the run context.

    Populated on the worker path; usually empty on the HTTP fast path, which has
    no ``run_id`` (that is one of the reasons the fast path is not inside the
    durable HITL boundary — see ``docs/interview/hitl-deep-dive.md``).
    """
    attrs: dict[str, Any] = {}
    try:
        from runtime.context import (
            get_current_run_id,
            get_current_task_id,
            get_current_thread_id,
        )

        run_id = get_current_run_id()
        if run_id:
            attrs["csai.run_id"] = run_id
        thread_id = get_current_thread_id()
        if thread_id:
            attrs["csai.thread_id"] = thread_id
        task_id = get_current_task_id()
        if task_id:
            attrs["csai.task_id"] = task_id
    except Exception:  # noqa: BLE001
        pass
    trace_id = current_trace_id()
    if trace_id:
        attrs["csai.trace_id"] = trace_id
    return scrub_attributes(attrs)


__all__ = [
    "ALLOWED_ATTRIBUTES",
    "FORBIDDEN_SUBSTRINGS",
    "current_trace_id",
    "enabled",
    "run_correlation_attributes",
    "scrub_attributes",
    "span",
]
