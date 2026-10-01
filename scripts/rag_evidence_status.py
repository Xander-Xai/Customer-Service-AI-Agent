#!/usr/bin/env python3
"""Shared RAG formal-metric status derivation (single source of truth).

Evidence direction (v3 governance fix):

    artifact/evidence -> facts -> docs -> guard

The formal 649-query metric status is derived ONLY from provenance-bearing
evidence artifacts under ``artifacts/evaluation/rag-649/**/report.json``.
Markdown text (docs/reference/*.md) never counts as evidence: docs are a
rendered projection of the artifact state and are verified against it by
``scripts/audit_doc_consistency.py`` / ``scripts/project_facts.py --check``.

A report.json counts as a formal full-run artifact only when ALL of the
following hold (fields taken from the real ``scripts/evaluate_rag.py``
schema; this module invents no second schema):

- parseable JSON with ``schema_version`` matching ``rag-eval-evidence/v<n>``
- top-level ``status`` == "VERIFIED_FULL" (the 4-config ablation
  contract: canonical-experiment completeness is enforced below;
  VERIFIED_FULL_NO_RERANK, SUBSET_SMOKE, PARTIAL and BLOCKED*/gated
  artifacts are rejected structurally, not by directory/file naming)
- ``subset_run`` is exactly False
- ``benchmark.sha256`` equals the sha256 of the CURRENT
  ``tests/eval/rag_benchmark.json`` (provenance binding to the live
  benchmark asset)
- ``benchmark.declared_queries == actual_queries == executed_queries``
  and all equal the current benchmark's ``metadata.total_queries``
- provenance present and non-empty: ``git_sha``, parseable ISO ``timestamp``
- all canonical harness experiments (extracted from the evaluate_rag.py
  source) have non-empty ``metrics`` and a ``run_summary`` entry whose
  ``n_success`` equals the executed query count (full-run contract)
- ``evaluation_populations.primary_view == "all_queries"`` with counts
  (v2 population contract)

Without such an artifact the status is NOT_VERIFIED (fail-closed).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from eval_contract import EXPERIMENT_NAMES  # noqa: E402

RELATIVE_REFERENCE_DIRS = ("artifacts", "evaluation", "rag-649")

STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_VERIFIED = "NOT_VERIFIED"
STATUS_VERIFIED = "VERIFIED"

REPORT_SCHEMA_VERSION_RE = re.compile(r"^rag-eval-evidence/v[1-9]+$")

# Canonical formal contract: the 4-config ablation (vector_only /
# bm25_only / hybrid_no_rerank / hybrid_rerank) — backed by validate_formal_
# rag_report requiring every canonical experiment run. VERIFIED_FULL_NO_RERANK
# means the reranker ablation leg is missing (evaluate_rag.py can exclude
# hybrid_rerank while still marking the remaining run full), which does NOT
# constitute complete formal evidence: status stays NOT_VERIFIED (fail-closed).
FORMAL_RUN_STATUSES = frozenset({"VERIFIED_FULL"})


def _failure() -> dict:
    return {
        "rag_formal_metrics_status": STATUS_NOT_VERIFIED,
        "rag_formal_artifact_path": None,
        "rag_formal_artifact_timestamp": None,
        "rag_formal_artifact_git_sha": None,
        "rag_formal_artifact_schema_version": None,
    }


def harness_experiment_names(root: Path = PROJECT_ROOT) -> list[str]:
    """Canonical experiment names from the shared evaluation contract.

    ``scripts/eval_contract.py`` is the single source of truth, imported by the
    evaluator itself — no second contract definition and no fragile regex over
    the harness source.
    """
    return list(EXPERIMENT_NAMES)


def _sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _benchmark_facts(root: Path) -> tuple[str | None, int | None]:
    bench = root / "tests" / "eval" / "rag_benchmark.json"
    if not bench.exists():
        return None, None
    try:
        data = json.loads(bench.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    sha = _sha256_file(bench)
    declared = data.get("metadata", {}).get("total_queries")
    if not isinstance(declared, int):
        declared = None
    return sha, declared


def validate_formal_rag_report(
    report_path: Path,
    root: Path = PROJECT_ROOT,
    experiments: list[str] | None = None,
    benchmark_sha: str | None = None,
    declared_queries: int | None = None,
) -> dict[str, Any] | None:
    """Structurally validate candidate report.json against the formal contract.

    Returns an info dict on success, or None when the artifact is rejected
    (reject reasons are structural; a rejected artifact never promotes to
    VERIFIED). The reasons are only meant for the caller's logs.
    """
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(report, dict):
        return None

    schema_version = report.get("schema_version")
    if not isinstance(schema_version, str) or not REPORT_SCHEMA_VERSION_RE.fullmatch(
        schema_version
    ):
        return None

    status = report.get("status")
    if status not in FORMAL_RUN_STATUSES:
        return None

    if report.get("subset_run") is not False:
        return None

    git_sha = report.get("git_sha")
    if not isinstance(git_sha, str) or not git_sha.strip():
        return None

    timestamp = report.get("timestamp")
    if not isinstance(timestamp, str):
        return None
    try:
        parsed_ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None

    benchmark = report.get("benchmark")
    if not isinstance(benchmark, dict):
        return None
    artifact_bench_sha = benchmark.get("sha256")
    if not isinstance(artifact_bench_sha, str) or not artifact_bench_sha:
        return None
    if benchmark_sha is not None and artifact_bench_sha != benchmark_sha:
        return None  # provenance must bind to the CURRENT benchmark asset
    counts = [benchmark.get(key) for key in ("declared_queries", "actual_queries", "executed_queries")]
    if any(not isinstance(c, int) for c in counts):
        return None
    if declared_queries is None or len(set(counts)) != 1 or counts[0] != declared_queries:
        return None

    if not experiments:
        return None
    metrics = report.get("metrics")
    run_summary = report.get("run_summary")
    if not isinstance(metrics, dict) or not isinstance(run_summary, dict):
        return None
    executed = counts[0]
    for name in experiments:
        experiment_metrics = metrics.get(name)
        if not isinstance(experiment_metrics, dict) or not experiment_metrics:
            return None
        summary = run_summary.get(name)
        if not isinstance(summary, dict):
            return None
        if summary.get("n_success") != executed:
            return None  # partial execution is not a formal full run

    populations = report.get("evaluation_populations")
    if not isinstance(populations, dict):
        return None
    if populations.get("primary_view") != "all_queries":
        return None
    if not isinstance(populations.get("counts"), dict) or not populations["counts"]:
        return None

    info = {
        "path": report_path,
        "timestamp": timestamp,
        "parsed_timestamp": parsed_ts,
        "git_sha": git_sha,
        "schema_version": schema_version,
        "run_status": status,
    }
    return info


FORMAL_STATUS_MARKER = "当前 649-query 正式指标"
_DOC_STATE_RE = re.compile(r"NOT_VERIFIED|(?<![A-Z_])VERIFIED(?![A-Z_])")


def claimed_doc_formal_status(text: str) -> str | None:
    """Read the formal-status claims a doc makes, WITHOUT treating them truth.

    Returns "NOT_VERIFIED" / "VERIFIED" when the doc is internally consistent,
    "CONFLICT" when it claims both, and None when the doc stays silent.
    Used only to compare the doc projection against the artifact-derived state.
    """
    states: set[str] = set()
    for line in text.splitlines():
        if FORMAL_STATUS_MARKER not in line:
            continue
        # Negated phrasings ("不是 VERIFIED" is not possible in our docs; but
        # NOT_VERIFIED contains VERIFIED) — strip NOT_VERIFIED first.
        if "NOT_VERIFIED" in line:
            states.add(STATUS_NOT_VERIFIED)
        else:
            match = _DOC_STATE_RE.search(line)
            if match:
                states.add(match.group(0))
    if not states:
        return None
    if len(states) == 2:
        return "CONFLICT"
    return next(iter(states))


def derive_rag_formal_status(root: Path = PROJECT_ROOT) -> dict:
    """Derive the formal-metric status from the newest valid formal artifact.

    Returns a project-facts style projection:
    ``{rag_formal_metrics_status, rag_formal_artifact_path,
       rag_formal_artifact_timestamp, rag_formal_artifact_git_sha,
       rag_formal_artifact_schema_version}``.
    """
    experiments = harness_experiment_names(root)
    benchmark_sha, declared_queries = _benchmark_facts(root)

    candidates = []
    artifact_dir = root.joinpath(*RELATIVE_REFERENCE_DIRS)
    for report_path in sorted(artifact_dir.glob("*/report.json")):
        info = validate_formal_rag_report(
            report_path,
            root=root,
            experiments=experiments,
            benchmark_sha=benchmark_sha,
            declared_queries=declared_queries,
        )
        if info is not None:
            candidates.append(info)

    if not candidates:
        return _failure()

    latest = max(candidates, key=lambda item: item["parsed_timestamp"])
    return {
        "rag_formal_metrics_status": STATUS_VERIFIED,
        "rag_formal_artifact_path": latest["path"].relative_to(root).as_posix(),
        "rag_formal_artifact_timestamp": latest["timestamp"],
        "rag_formal_artifact_git_sha": latest["git_sha"],
        "rag_formal_artifact_schema_version": latest["schema_version"],
    }


if __name__ == "__main__":
    import json as _json
    import sys as _sys

    _sys.stdout.write(
        _json.dumps(derive_rag_formal_status(), ensure_ascii=False, indent=2) + "\n"
    )
