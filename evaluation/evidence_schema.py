"""Serializable evidence records with explicit provenance and missing values.

This module deliberately does not infer production facts from local estimates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EvidenceSource(str, Enum):
    PROVIDER_REPORTED = "PROVIDER_REPORTED"
    APPLICATION_MEASURED = "APPLICATION_MEASURED"
    ESTIMATED = "ESTIMATED"
    FIXTURE = "FIXTURE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    NOT_MEASURED = "NOT_MEASURED"
    NOT_VERIFIED = "NOT_VERIFIED"


@dataclass(frozen=True)
class Measurement:
    value: float | int | str | None
    source: EvidenceSource
    unit: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        missing_sources = {
            EvidenceSource.NOT_AVAILABLE,
            EvidenceSource.NOT_MEASURED,
            EvidenceSource.NOT_VERIFIED,
        }
        if self.source in missing_sources and self.value is not None:
            raise ValueError(f"{self.source.value} measurements must have value=None")
        if self.source not in missing_sources and self.value is None:
            raise ValueError(f"{self.source.value} measurements require a value")

    @classmethod
    def unavailable(cls, source: EvidenceSource, notes: str | None = None) -> Measurement:
        if source not in {
            EvidenceSource.NOT_AVAILABLE,
            EvidenceSource.NOT_MEASURED,
            EvidenceSource.NOT_VERIFIED,
        }:
            raise ValueError("unavailable() requires a missing-value source")
        return cls(value=None, source=source, notes=notes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source.value,
            "unit": self.unit,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class LatencySummary:
    sample_count: int
    warmup_count: int
    percentile_method: str
    source: EvidenceSource
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    ttft_ms: float | None = None
    total_completion_ms: float | None = None

    def __post_init__(self) -> None:
        if self.sample_count < 0 or self.warmup_count < 0:
            raise ValueError("sample counts cannot be negative")
        if self.source in {
            EvidenceSource.NOT_MEASURED,
            EvidenceSource.NOT_VERIFIED,
            EvidenceSource.NOT_AVAILABLE,
        } and any(value is not None for value in (self.p50_ms, self.p95_ms, self.p99_ms)):
            raise ValueError("missing latency sources cannot contain percentile values")

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "warmup_count": self.warmup_count,
            "percentile_method": self.percentile_method,
            "source": self.source.value,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "ttft_ms": self.ttft_ms,
            "total_completion_ms": self.total_completion_ms,
        }


@dataclass
class EvidenceRecord:
    run_id: str
    timestamp: str
    git_sha: str
    environment: str
    provider: str | None
    model: str | None
    workload_id: str
    scenario: str
    request_count: int
    success_count: int
    failure_count: int
    latency: LatencySummary
    input_tokens: Measurement
    output_tokens: Measurement
    cached_tokens: Measurement
    provider_cost: Measurement
    component_latency: dict[str, LatencySummary] = field(default_factory=dict)
    estimated_tokens: Measurement = field(
        default_factory=lambda: Measurement.unavailable(EvidenceSource.NOT_AVAILABLE)
    )
    retrieval_channels: dict[str, int | float | str | None] = field(default_factory=dict)
    task_success: dict[str, int | float | str | None] = field(default_factory=dict)
    recoverability: dict[str, int | float | str | None] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    estimated_cost: Measurement = field(
        default_factory=lambda: Measurement.unavailable(EvidenceSource.NOT_AVAILABLE)
    )
    provider_request_id: str | None = None
    attempt_count: int | None = None
    retry_count: int | None = None
    timeout_count: int | None = None
    provider_error_code: str | None = None
    final_status: str | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.request_count < 0 or self.success_count < 0 or self.failure_count < 0:
            raise ValueError("request counters cannot be negative")
        if self.success_count + self.failure_count != self.request_count:
            raise ValueError("success_count + failure_count must equal request_count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "git_sha": self.git_sha,
            "environment": self.environment,
            "provider": self.provider,
            "model": self.model,
            "workload_id": self.workload_id,
            "scenario": self.scenario,
            "request_count": self.request_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "latency": self.latency.to_dict(),
            "component_latency": {
                name: summary.to_dict() for name, summary in self.component_latency.items()
            },
            "estimated_tokens": self.estimated_tokens.to_dict(),
            "input_tokens": self.input_tokens.to_dict(),
            "output_tokens": self.output_tokens.to_dict(),
            "cached_tokens": self.cached_tokens.to_dict(),
            "provider_input_tokens": self.input_tokens.to_dict(),
            "provider_output_tokens": self.output_tokens.to_dict(),
            "provider_cached_tokens": self.cached_tokens.to_dict(),
            "provider_cost": self.provider_cost.to_dict(),
            "estimated_cost": self.estimated_cost.to_dict(),
            "provider_request_id": self.provider_request_id,
            "attempt_count": self.attempt_count,
            "retry_count": self.retry_count,
            "timeout_count": self.timeout_count,
            "provider_error_code": self.provider_error_code,
            "final_status": self.final_status,
            "samples": self.samples,
            "retrieval_channels": self.retrieval_channels,
            "task_success": self.task_success,
            "recoverability": self.recoverability,
            "metadata": self.metadata,
            "notes": self.notes,
        }
