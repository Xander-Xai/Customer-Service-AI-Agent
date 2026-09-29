"""Deterministic metric calculations with explicit zero-sample behavior."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .evidence_schema import EvidenceSource, LatencySummary


def percentile(values: Sequence[float], percentile_value: float) -> float:
    """Return an interpolated percentile; P99 is not silently replaced by max."""
    if not values:
        raise ValueError("percentile requires at least one sample")
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(float(value) for value in values)
    rank = (len(ordered) - 1) * percentile_value / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def summarize_latency(
    samples_ms: Sequence[float],
    *,
    warmup_count: int,
    source: EvidenceSource,
    percentile_method: str = "linear_interpolation_rank_(n-1)*p",
) -> LatencySummary:
    if warmup_count < 0 or warmup_count > len(samples_ms):
        raise ValueError("warmup_count must be within the sample sequence")
    measured = list(samples_ms)[warmup_count:]
    if not measured:
        return LatencySummary(
            sample_count=0,
            warmup_count=warmup_count,
            percentile_method=percentile_method,
            source=EvidenceSource.NOT_MEASURED,
            p50_ms=None,
            p95_ms=None,
            p99_ms=None,
        )
    return LatencySummary(
        sample_count=len(measured),
        warmup_count=warmup_count,
        percentile_method=percentile_method,
        source=source,
        p50_ms=round(percentile(measured, 50), 6),
        p95_ms=round(percentile(measured, 95), 6),
        p99_ms=round(percentile(measured, 99), 6),
    )


def retrieval_metrics(expected_doc_ids: Sequence[str], ranked_doc_ids: Sequence[str], top_k: int) -> dict[str, Any]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    expected = set(expected_doc_ids)
    ranked = list(ranked_doc_ids[:top_k])
    hits = [doc_id for doc_id in ranked if doc_id in expected]
    reciprocal_rank = next((1 / (index + 1) for index, doc_id in enumerate(ranked) if doc_id in expected), 0.0)
    return {
        "sample_count": 1,
        "hit_at_k": int(bool(hits)),
        "recall_at_k": len(set(hits)) / len(expected) if expected else None,
        "mrr": reciprocal_rank,
    }
