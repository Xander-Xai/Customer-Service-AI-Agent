"""Local evidence runner and future external-system seams."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evidence_schema import EvidenceRecord, EvidenceSource, LatencySummary, Measurement
from .metrics import retrieval_metrics, summarize_latency
from .workloads import WorkloadCase, local_workloads


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "NOT_AVAILABLE"


def _config_hash(workloads: tuple[WorkloadCase, ...]) -> str:
    payload = json.dumps([case.__dict__ for case in workloads], sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _task_success(case: WorkloadCase) -> bool:
    evidence_ok = set(case.expected_doc_ids).intersection(case.ranked_doc_ids)
    fields_ok = set(case.expected_fields).issubset(case.observed_fields)
    tool_ok = case.expected_tool is None or case.expected_tool == case.observed_tool
    degraded_ok = (
        case.expected_degraded_signal is None
        or case.expected_degraded_signal == case.observed_degraded_signal
    )
    return (
        (not case.expected_doc_ids or bool(evidence_ok)) and fields_ok and tool_ok and degraded_ok
    )


def _unverified_record(suite: str) -> dict[str, Any]:
    missing = Measurement.unavailable(
        EvidenceSource.NOT_VERIFIED, f"{suite} adapter disabled; no external call made"
    )
    return {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "environment": "NOT_VERIFIED",
        "provider": None,
        "model": None,
        "workload_id": f"{suite}-external",
        "scenario": suite,
        "request_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "latency": {
            "sample_count": 0,
            "warmup_count": 0,
            "percentile_method": "not_run",
            "source": EvidenceSource.NOT_VERIFIED.value,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "ttft_ms": None,
            "total_completion_ms": None,
        },
        "component_latency": {},
        "estimated_tokens": missing.to_dict(),
        "input_tokens": missing.to_dict(),
        "output_tokens": missing.to_dict(),
        "cached_tokens": missing.to_dict(),
        "provider_cost": missing.to_dict(),
        "retrieval_channels": {},
        "task_success": {"status": EvidenceSource.NOT_VERIFIED.value},
        "recoverability": {"status": EvidenceSource.NOT_VERIFIED.value},
        "metadata": {"suite": suite, "external_calls": False},
        "notes": ["External adapter is disabled; this record is not production evidence."],
    }


def run_local(*, repeat: int = 3, warmup: int = 1) -> EvidenceRecord:
    if repeat <= 0:
        raise ValueError("repeat must be positive; zero-request PASS is forbidden")
    if warmup < 0:
        raise ValueError("warmup cannot be negative")
    workloads = local_workloads()
    latencies: list[float] = []
    outcomes: list[bool] = []
    recoverability = Counter[str]()
    hit_sum = 0.0
    recall_sum = 0.0
    mrr_sum = 0.0
    retrieval_sample_count = 0
    for _ in range(warmup + repeat):
        for case in workloads:
            started = time.perf_counter()
            result = _task_success(case)
            latencies.append((time.perf_counter() - started) * 1000)
            if _ >= warmup:
                outcomes.append(result)
                recoverability[case.recoverability] += 1
                if case.expected_doc_ids:
                    metrics = retrieval_metrics(case.expected_doc_ids, case.ranked_doc_ids, top_k=3)
                    hit_sum += metrics["hit_at_k"]
                    recall_sum += metrics["recall_at_k"] or 0.0
                    mrr_sum += metrics["mrr"]
                    retrieval_sample_count += 1
    measured_samples = len(workloads) * repeat
    # Hit@K / Recall@K / MRR are preserved with their sample count. The
    # per-channel (vector/BM25) attribution is genuinely NOT_MEASURED: a
    # WorkloadCase carries only one combined ranked_doc_ids list, so claiming
    # vector_only/bm25_only outcomes would be fabricated attribution.
    channels: dict[str, int | float | str | None] = {
        "hit_at_k": round(hit_sum / retrieval_sample_count, 4) if retrieval_sample_count else None,
        "recall_at_k": round(recall_sum / retrieval_sample_count, 4)
        if retrieval_sample_count
        else None,
        "mrr": round(mrr_sum / retrieval_sample_count, 4) if retrieval_sample_count else None,
        "retrieval_sample_count": retrieval_sample_count,
        "vector_only_hit": "NOT_MEASURED",
        "bm25_only_hit": "NOT_MEASURED",
        "both_hit": "NOT_MEASURED",
        "neither_hit": "NOT_MEASURED",
        "per_channel_note": (
            "per-channel vector/BM25 attribution is NOT_MEASURED: WorkloadCase "
            "exposes only a combined ranked_doc_ids list"
        ),
    }
    latency = summarize_latency(
        latencies,
        warmup_count=len(workloads) * warmup,
        source=EvidenceSource.FIXTURE,
    )
    not_measured_component = LatencySummary(
        sample_count=0,
        warmup_count=0,
        percentile_method="not_run",
        source=EvidenceSource.NOT_MEASURED,
        p50_ms=None,
        p95_ms=None,
        p99_ms=None,
    )
    success_count = sum(outcomes)
    record = EvidenceRecord(
        run_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc).isoformat(),
        git_sha=_git_sha(),
        environment="LOCAL_FIXTURE",
        provider=None,
        model=None,
        workload_id="local-production-evidence-v1",
        scenario="mixed_local_workloads",
        request_count=measured_samples,
        success_count=success_count,
        failure_count=measured_samples - success_count,
        latency=latency,
        component_latency={
            "e2e": latency,
            "ttft": not_measured_component,
            "retrieval": not_measured_component,
            "rerank": not_measured_component,
            "llm": not_measured_component,
            "tool": not_measured_component,
        },
        estimated_tokens=Measurement.unavailable(
            EvidenceSource.NOT_AVAILABLE, "fixture has no token tracker input"
        ),
        input_tokens=Measurement.unavailable(
            EvidenceSource.NOT_AVAILABLE, "fixture has no provider response"
        ),
        output_tokens=Measurement.unavailable(
            EvidenceSource.NOT_AVAILABLE, "fixture has no provider response"
        ),
        cached_tokens=Measurement.unavailable(
            EvidenceSource.NOT_AVAILABLE, "fixture has no provider response"
        ),
        provider_cost=Measurement.unavailable(
            EvidenceSource.NOT_AVAILABLE, "fixture has no provider billing record"
        ),
        retrieval_channels=dict(channels),
        task_success={"passed": success_count, "failed": measured_samples - success_count},
        recoverability=dict(recoverability),
        metadata={
            "python": sys.version.split()[0],
            "os": platform.platform(),
            "workload_version": "local-v1",
            "config_hash": _config_hash(workloads),
            "sample_count": measured_samples,
            "warmup_count": len(workloads) * warmup,
            "external_calls": False,
        },
        notes=["LOCAL_ONLY: deterministic fixture contract; not production evidence."],
    )
    return record


def _provider_measurement_present(payload: dict[str, Any]) -> bool:
    for key in ("provider_cost", "input_tokens", "output_tokens", "cached_tokens"):
        measurement = payload.get(key)
        if isinstance(measurement, dict) and measurement.get("source") == "PROVIDER_REPORTED":
            return True
    return False


def render_markdown(payload: dict[str, Any]) -> str:
    """Render Markdown from the payload, never from hardcoded assumptions.

    The JSON and Markdown artifacts must agree on environment, run id, git SHA,
    request/success/failure counts, status and provider-measurement
    availability. A controlled staging run that collected provider-reported
    measurements must not print "NOT_VERIFIED"; a local fixture must not imply
    production evidence.
    """
    latency = payload["latency"]
    environment = payload.get("environment", "UNKNOWN")
    final_status = payload.get("final_status")
    if environment == "LOCAL_FIXTURE":
        status = "LOCAL_ONLY"
    elif final_status:
        status = final_status
    else:
        status = environment
    if _provider_measurement_present(payload):
        boundary = (
            "Provider-reported token/usage measurements are present for this controlled "
            "staging run; provider billing, production latency/SLA and production "
            "task-success remain `NOT_VERIFIED`."
        )
    else:
        boundary = (
            "Provider tokens, provider billing, Redis, ERP, Qdrant migration, and "
            "production behavior remain `NOT_VERIFIED`."
        )
    return "\n".join(
        [
            "# Production Evidence Run",
            "",
            f"- status: `{status}`",
            f"- environment: `{environment}`",
            f"- run_id: `{payload['run_id']}`",
            f"- git_sha: `{payload['git_sha']}`",
            f"- workload: `{payload['workload_id']}`",
            f"- requests: `{payload['request_count']}`",
            f"- task success: `{payload['success_count']}/{payload['request_count']}`",
            f"- latency source: `{latency['source']}`",
            f"- latency P50/P95/P99 ms: `{latency['p50_ms']}` / `{latency['p95_ms']}` / `{latency['p99_ms']}`",
            "",
            boundary,
            "",
        ]
    )


def write_run(payload: dict[str, Any], output: Path, markdown_output: Path | None = None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if markdown_output is not None:
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.write_text(render_markdown(payload), encoding="utf-8")
