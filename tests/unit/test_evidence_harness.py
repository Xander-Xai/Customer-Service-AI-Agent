from __future__ import annotations

import pytest

from evaluation.evidence_schema import EvidenceSource, Measurement
from evaluation.metrics import percentile, summarize_latency
from evaluation.runner import run_local
from evaluation.workloads import local_workloads


def test_percentile_uses_interpolation_not_max() -> None:
    assert percentile([10, 20, 30, 40], 50) == 25
    assert percentile([10, 20, 30, 40], 99) < 40


def test_zero_samples_are_not_reported_as_zero() -> None:
    summary = summarize_latency([], warmup_count=0, source=EvidenceSource.FIXTURE)
    assert summary.sample_count == 0
    assert summary.p99_ms is None
    assert summary.source is EvidenceSource.NOT_MEASURED


def test_measurement_rejects_missing_value_as_metric() -> None:
    with pytest.raises(ValueError):
        Measurement(0, EvidenceSource.NOT_AVAILABLE)


def test_local_run_has_repeated_fixture_samples_and_explicit_missing_provider_data() -> None:
    record = run_local(repeat=2, warmup=1)
    assert record.request_count == len(local_workloads()) * 2
    assert record.success_count == record.request_count
    assert record.latency.sample_count == len(local_workloads()) * 2
    assert record.input_tokens.source is EvidenceSource.NOT_AVAILABLE
    assert record.provider_cost.source is EvidenceSource.NOT_AVAILABLE
    assert record.recoverability["degraded_success"] == 6
