"""P0-05: Embedding dependency failure contract & retrieval result metadata.

Prevents embedding dependency failure from being coerced into a synthetic
vector (random / deterministic-random / hash-derived). Instead, embedding
unavailability is surfaced as a typed state so the vector retrieval channel
can be explicitly disabled, with an observable degraded result.

States (EmbeddingStatus):
  AVAILABLE   — embed_fn present and produced a valid-dimension, finite vector
  UNAVAILABLE — no embed_fn, or encode() raised (provider down / not configured)
  INVALID     — encode() returned wrong dimension or non-finite values

This module contains ONLY types/helpers — no behavioral fallback. The
retrieval methods in rag.qdrant_knowledge_base and the semantic cache in
cache.response_cache consume this contract to fail closed instead of
fabricating vectors.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class EmbeddingStatus(str, Enum):
    """Embedding channel state, distinguishable by callers."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"

    @classmethod
    def from_error(cls, exc: BaseException) -> EmbeddingStatus:
        """Map a typed embedding error to its channel state.

        AVAILABLE is a pre-check state (embed_fn present); UNAVAILABLE and
        INVALID are per-call outcomes derived from the typed error. This makes
        INVALID reachable — a wrong-dimension / non-finite / non-numeric
        embedding is reported as INVALID, distinct from UNAVAILABLE.
        """
        if isinstance(exc, EmbeddingDimensionError):
            return cls.INVALID
        return cls.UNAVAILABLE


class EmbeddingUnavailableError(RuntimeError):
    """Embedding provider unavailable — disable the vector channel, do not fake it.

    Raised when no embed_fn is configured (provider_unavailable) or when
    encode() raises at runtime (encode_failed). The vector retrieval channel
    must be explicitly disabled; a BM25/lexical fallback may run if ready.
    """

    def __init__(
        self,
        reason: str = "provider_unavailable",
        *,
        provider: str = "",
        model: str = "",
    ) -> None:
        self.reason = reason
        self.provider = provider
        self.model = model
        super().__init__(
            f"embedding unavailable: {reason} (provider={provider!r}, model={model!r})"
        )


class EmbeddingDimensionError(ValueError):
    """Embedding returned an invalid vector — reject, do not pad/truncate/random-fill.

    Covers the INVALID state: wrong dimension, empty vector, or non-finite
    (NaN/Inf) values. Never silently reshaped to satisfy Qdrant.
    """

    def __init__(
        self,
        expected: int,
        actual: int | str,
        *,
        model: str = "",
        reason: str = "dimension_mismatch",
    ) -> None:
        self.expected = expected
        self.actual = actual
        self.model = model
        self.reason = reason
        super().__init__(
            f"embedding invalid: {reason} expected={expected} actual={actual} "
            f"(model={model!r})"
        )


def validate_embedding_vector(
    vector: list[float], expected_dim: int, *, model: str = ""
) -> None:
    """Validate a single embedding vector; raise EmbeddingDimensionError if invalid.

    Checks: non-empty, correct dimension, finite (no NaN/Inf), numeric elements.
    Never pads, truncates, or random-fills — invalid vectors are rejected so
    that Qdrant is never queried with a malformed vector.
    """
    actual = len(vector)
    if actual == 0:
        raise EmbeddingDimensionError(expected_dim, 0, model=model, reason="empty_vector")
    if actual != expected_dim:
        raise EmbeddingDimensionError(
            expected_dim, actual, model=model, reason="dimension_mismatch"
        )
    # Non-finite / non-numeric check — reject, do not silently reshape.
    # Non-numeric elements (string/None/...) are wrapped into the typed
    # INVALID contract rather than propagating a bare ValueError.
    for v in vector:
        try:
            f = float(v)
        except (ValueError, TypeError) as e:
            raise EmbeddingDimensionError(
                expected_dim, "non_numeric", model=model, reason="non_numeric"
            ) from e
        if f != f or f in (float("inf"), float("-inf")):  # NaN or Inf
            raise EmbeddingDimensionError(
                expected_dim, "non_finite", model=model, reason="non_finite"
            )


def degraded_reason_for(exc: BaseException) -> str:
    """Per-query degraded_reason for a typed embedding error.

    Distinguishes an INVALID embedding (wrong dim / non-finite / non-numeric)
    from an UNAVAILABLE one (no embed_fn / encode() raised), so caller metadata
    is accurate instead of labeling every failure ``embedding_unavailable``.
    """
    if isinstance(exc, EmbeddingDimensionError):
        return "embedding_invalid"
    return "embedding_unavailable"


class RetrievalResultList(list):
    """A retrieval result list carrying per-query degraded metadata.

    IS-A list (backward compatible: len(), iteration, indexing, slicing,
    isinstance(list) all behave normally). Callers that care about degradation
    read ``.meta``; callers that just iterate results are unaffected.

    ``.meta`` defaults to a non-degraded marker so the attribute always exists.

    Typical meta keys (see QdrantKnowledgeBase.query_multiple):
        retrieval_degraded: bool
        degraded_reason: str   # embedding_unavailable | no_retrieval_channel | ...
        vector_channel_used: bool
        lexical_channel_used: bool
        embedding_provider: str
        embedding_model: str
    """

    def __init__(self, seq: Any = (), *, meta: dict[str, Any] | None = None) -> None:
        super().__init__(seq)
        self.meta: dict[str, Any] = (
            dict(meta) if meta else {"retrieval_degraded": False}
        )

    @property
    def retrieval_degraded(self) -> bool:
        return bool(self.meta.get("retrieval_degraded"))


# Reason codes (shared constants so log/metric labels stay consistent).
REASON_PROVIDER_UNAVAILABLE = "provider_unavailable"
REASON_ENCODE_FAILED = "encode_failed"
REASON_EMBEDDING_UNAVAILABLE = "embedding_unavailable"
REASON_NO_RETRIEVAL_CHANNEL = "no_retrieval_channel"
