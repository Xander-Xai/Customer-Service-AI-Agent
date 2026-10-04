"""Reverse tests for the RAG 649 evidence-validity gate (issue #45).

Every test here encodes a way the pipeline could previously certify worthless
evidence. The four scenarios the gate must reject are covered end-to-end:

1. 100% degraded retrieval (channel dead for the whole population)
2. gold corpus missing (corpus import never landed)
3. nominal full run (must still certify — the gate is not a blanket deny)
4. subset run (a smoke/diagnostic artifact is not formal evidence)

The predicate reads only execution / corpus / channel facts, never metric
magnitude: ``test_predicate_ignores_metric_magnitude`` pins that down, because a
legitimately weak retriever must still be able to produce VERIFIED evidence.

Layers under test:

- ``assess_evidence_validity`` — the predicate itself
- ``evaluate_rag`` — the producer: rows -> observations -> status label
- ``rag_evidence_status`` — the consumer: artifact -> headline VERIFIED /
  NOT_VERIFIED (recomputed, never read from the artifact's own status field)
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

spec = importlib.util.spec_from_file_location("evaluate_rag", SCRIPTS_DIR / "evaluate_rag.py")
ev = importlib.util.module_from_spec(spec)
sys.modules["evaluate_rag"] = ev
spec.loader.exec_module(ev)

import rag_evidence_status  # noqa: E402
from eval_contract import (  # noqa: E402
    CHANNELS,
    EXPERIMENT_NAMES,
    REPORT_SCHEMA_VERSION,
    VALIDITY_CONTRACT_VERSION,
    VERDICT_INVALID,
    VERDICT_VALID,
    required_channels,
)
from rag_evidence_validity import (  # noqa: E402
    C01_FULLY_DEGRADED,
    C03_REQUIRED_CHANNEL_NEVER_USED,
    C04_REQUIRED_CHANNEL_EMPTY,
    C05_PREFLIGHT_NOT_CLEAN,
    E01_SUBSET_RUN,
    E02_QUERY_COUNT_INVALID,
    E03_CANONICAL_LEG_MISSING,
    E04_REQUEST_ERRORS,
    E05_EXECUTION_INCOMPLETE,
    G01_GOLD_NOT_INDEXED_ABOVE_LIMIT,
    G02_RETRIEVAL_ELIGIBLE_BELOW_FLOOR,
    G03_FULL_GOLD_COVERED_BELOW_FLOOR,
    S00_OBSERVATIONS_MALFORMED,
    assess_evidence_validity,
    observations_are_consistent,
    reason_codes,
)

N = 649  # the formal population size; counts come from the benchmark metadata


# ---------------------------------------------------------------- observations


def _channels(
    name: str,
    *,
    used: int,
    candidates: int = 32,
) -> dict[str, Any]:
    """Channel facts for one leg. ``used`` is the healthy case's usage count."""
    required = set(required_channels(name))
    out: dict[str, Any] = {}
    for channel in CHANNELS:
        engaged = channel in required and used > 0
        out[channel] = {
            "required": channel in required,
            "used_count": used if engaged else 0,
            "executed_count": used if engaged else 0,
            "candidate_total": (candidates * used) if engaged else 0,
        }
    return out


def _leg(
    name: str,
    *,
    n_total: int = N,
    n_success: int = N,
    n_error: int = 0,
    n_degraded: int = 0,
    gold_not_indexed: int = 0,
    channel_used: int = N,
    candidates: int = 32,
) -> dict[str, Any]:
    return {
        "n_total": n_total,
        "n_success": n_success,
        "n_error": n_error,
        "n_degraded": n_degraded,
        "failure_counts": {"GOLD_NOT_INDEXED": gold_not_indexed},
        "channels": _channels(name, used=channel_used, candidates=candidates),
    }


def _observations(
    *,
    subset_run: bool = False,
    declared: int = N,
    executed: int = N,
    preflight_status: str = "OK",
    eligible: int = N,
    covered: int = N,
    legs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "subset_run": subset_run,
        "declared_queries": declared,
        "executed_queries": executed,
        "preflight_status": preflight_status,
        "population_counts": {
            "all_queries": executed,
            "retrieval_eligible": eligible,
            "full_gold_covered": covered,
        },
        "experiments": legs
        if legs is not None
        else {name: _leg(name) for name in EXPERIMENT_NAMES},
    }


def _codes(validity: dict[str, Any]) -> list[str]:
    return reason_codes(validity)


def _valid(observations: dict[str, Any]) -> dict[str, Any]:
    validity = assess_evidence_validity(observations)
    assert validity["verdict"] == VERDICT_VALID, validity["reasons"]
    assert validity["reasons"] == []
    return validity


def _invalid(observations: dict[str, Any]) -> dict[str, Any]:
    validity = assess_evidence_validity(observations)
    assert validity["verdict"] == VERDICT_INVALID
    assert validity["reasons"], "an INVALID verdict must carry machine-readable reasons"
    return validity


# ---------------------------------------------------------------- predicate


def test_nominal_full_run_is_valid() -> None:
    """Scenario 3 (healthy): a clean 4-leg run over a fully indexed corpus certifies."""
    validity = _valid(_observations())
    assert validity["contract"] == VALIDITY_CONTRACT_VERSION
    assert validity["thresholds"]["max_acceptable_degraded_ratio"] == 0.10
    assert validity["thresholds"]["min_retrieval_eligible_ratio"] == 0.90
    assert (
        validity["observed"]["experiments"]["hybrid_rerank"]["channels"]["rerank"]["used_count"]
        == N
    )


def test_all_degraded_run_is_invalid() -> None:
    """Scenario 1 (reverse): every leg answered on a dead/fallback channel."""
    observations = _observations(
        legs={
            name: _leg(
                name,
                n_degraded=N,
                channel_used=0,  # vector leg: channel never engaged
                candidates=0,
            )
            for name in EXPERIMENT_NAMES
        }
    )
    codes = _codes(_invalid(observations))
    assert C01_FULLY_DEGRADED in codes
    assert C03_REQUIRED_CHANNEL_NEVER_USED in codes
    assert C04_REQUIRED_CHANNEL_EMPTY in codes


def test_gold_corpus_missing_is_invalid() -> None:
    """Scenario 2 (reverse): 625/649 gold documents never indexed (issue #45 numbers)."""
    observations = _observations(
        eligible=24,  # only 24 queries have any indexed gold document
        covered=0,
        legs={name: _leg(name, gold_not_indexed=625) for name in EXPERIMENT_NAMES},
    )
    codes = _codes(_invalid(observations))
    assert G01_GOLD_NOT_INDEXED_ABOVE_LIMIT in codes
    assert G02_RETRIEVAL_ELIGIBLE_BELOW_FLOOR in codes
    assert G03_FULL_GOLD_COVERED_BELOW_FLOOR in codes


def test_subset_run_is_invalid() -> None:
    """Scenario 4 (reverse): 16-query smoke is a diagnostic, never formal evidence."""
    observations = _observations(subset_run=True, executed=16, eligible=16, covered=16)
    codes = _codes(_invalid(observations))
    assert E01_SUBSET_RUN in codes
    assert E02_QUERY_COUNT_INVALID in codes


def test_predicate_ignores_metric_magnitude() -> None:
    """Validity is about execution, not score: metrics are not an input at all.

    A retriever that hits nothing on a fully indexed corpus is a *valid*
    measurement of a bad retriever. If the predicate ever started reading
    ``metrics``, this assertion is what breaks.
    """
    observations = _observations()
    baseline = _valid(observations)
    polluted = _observations()
    polluted["metrics"] = {name: {"hit@3": 0.0, "mrr": 0.0} for name in EXPERIMENT_NAMES}
    polluted["metrics"]["hybrid_rerank"]["hit@3"] = 0.0
    polluted["run_summary_all_metrics_zero"] = True
    assert assess_evidence_validity(polluted)["verdict"] == baseline["verdict"] == VERDICT_VALID
    serialised = json.dumps(baseline, ensure_ascii=False)
    for family in ("hit@", "recall@", "ndcg@", "precision@", "mrr@"):
        assert family not in serialised


def test_partial_degradation_above_limit_is_invalid() -> None:
    """A minority fallback share is allowed; a large one mixes two regimes."""
    observations = _observations(
        legs={name: _leg(name, n_degraded=int(N * 0.5)) for name in EXPERIMENT_NAMES}
    )
    assert "C02_DEGRADED_RATIO_ABOVE_LIMIT" in _codes(_invalid(observations))
    healthy_minor = _observations(
        legs={name: _leg(name, n_degraded=8) for name in EXPERIMENT_NAMES}
    )
    _valid(healthy_minor)


def test_missing_canonical_leg_is_invalid() -> None:
    legs = {name: _leg(name) for name in EXPERIMENT_NAMES if name != "hybrid_rerank"}
    codes = _codes(_invalid(_observations(legs=legs)))
    assert E03_CANONICAL_LEG_MISSING in codes


def test_request_errors_and_incomplete_execution_are_invalid() -> None:
    legs = {name: _leg(name) for name in EXPERIMENT_NAMES}
    legs["hybrid_no_rerank"] = _leg("hybrid_no_rerank", n_success=N - 3, n_error=3)
    codes = _codes(_invalid(_observations(legs=legs)))
    assert E04_REQUEST_ERRORS in codes
    assert E05_EXECUTION_INCOMPLETE in codes


def test_preflight_not_clean_is_invalid() -> None:
    codes = _codes(_invalid(_observations(preflight_status="PARTIAL")))
    assert C05_PREFLIGHT_NOT_CLEAN in codes


def test_malformed_observations_fail_closed() -> None:
    assert _codes(_invalid({"experiments": "nope"}))  # type: ignore[arg-type]
    broken = _observations()
    broken["experiments"]["vector_only"]["n_total"] = "649"  # type: ignore[assignment]
    assert S00_OBSERVATIONS_MALFORMED in _codes(_invalid(broken))
    empty = _observations()
    empty["population_counts"]["all_queries"] = 0
    assert S00_OBSERVATIONS_MALFORMED in _codes(_invalid(empty))
    verdict = assess_evidence_validity("not-a-dict")  # type: ignore[arg-type]
    assert verdict["verdict"] == VERDICT_INVALID
    assert _codes(verdict) == [S00_OBSERVATIONS_MALFORMED]


def test_required_channels_come_from_the_ablation_specs() -> None:
    """A leg cannot weaken its own channel requirement."""
    assert required_channels("bm25_only") == ("bm25",)
    assert required_channels("vector_only") == ("vector",)
    assert required_channels("hybrid_no_rerank") == ("vector", "bm25")
    assert required_channels("hybrid_rerank") == ("vector", "bm25", "rerank")

    # the predicate derives the requirement from EXPERIMENT_SPECS, so hiding the
    # rerank facts does not make hybrid_rerank valid
    observations = _observations()
    observations["experiments"]["hybrid_rerank"]["channels"]["rerank"]["used_count"] = 0
    observations["experiments"]["hybrid_rerank"]["channels"]["rerank"]["candidate_total"] = 0
    assert C03_REQUIRED_CHANNEL_NEVER_USED in _codes(_invalid(observations))

    # and a producer that mislabels which channels it required is inconsistent
    lying = _observations()
    lying["experiments"]["hybrid_rerank"]["channels"]["rerank"]["required"] = False
    report = {
        "subset_run": False,
        "preflight": {"status": "OK"},
        "evaluation_populations": {"counts": lying["population_counts"]},
        "run_summary": {
            name: {
                "n_total": N,
                "n_success": N,
                "n_error": 0,
                "n_degraded": 0,
                "failure_counts": {"GOLD_NOT_INDEXED": 0},
            }
            for name in EXPERIMENT_NAMES
        },
    }
    assert not observations_are_consistent(lying, report, N)


# ---------------------------------------------------------------- producer


class _DeadChannelKB:
    """KB whose vector channel is dead but which still answers every request.

    This is the shape of the #45 artifact: ``retrieve`` returns, so the run
    counts 649/649 successes, while the vector leg never used the vector
    channel and every query was served by the fallback.
    """

    _embedding_model_name = "fake-embed"
    _embed_fn = object()

    def __init__(
        self,
        *,
        degraded: bool = True,
        fail_queries: frozenset[str] = frozenset(),
    ) -> None:
        self._hybrid_enabled = True
        self.degraded = degraded
        self.fail_queries = fail_queries

    async def retrieve(self, request):  # noqa: ANN001, ANN201 - test double
        from rag.retrieval_contract import (
            RetrievalResult,
            RetrievalTrace,
            StageStatus,
            TraceStage,
        )

        if request.query in self.fail_queries:
            raise TimeoutError("wall clock timeout")
        ids = [f"doc_{i}" for i in range(request.top_k)]
        trace = RetrievalTrace()
        for name, status, cand in (
            ("VECTOR", "degraded" if self.degraded else "executed", 0 if self.degraded else 32),
            ("BM25", "executed", 16),
            ("FUSION_RRF", "executed", 16),
            ("RERANK", "executed", 16),
        ):
            trace.add(
                TraceStage(
                    name=name,
                    status=StageStatus(status),
                    candidate_out=cand,
                    duration_ms=1.0,
                )
            )
        meta = {
            "retrieval_degraded": self.degraded,
            "degraded_reason": "embedding_unavailable" if self.degraded else "",
            "vector_channel_used": not self.degraded,
            "lexical_channel_used": True,
        }
        return RetrievalResult([{"id": i, "content": i} for i in ids], meta=meta, trace=trace)


def _queries(n: int, *, indexed_gold: bool = True) -> list[dict[str, Any]]:
    return [
        {
            "query_id": f"bench_{i:04d}",
            "query": f"查询 {i}",
            "expected_doc_ids": ["doc_0", "doc_1", "doc_2"],
            "category": "成分知识",
            "difficulty": "easy",
            "query_type": "single",
        }
        for i in range(n)
    ]


def _run_all_legs(kb, queries, corpus_ids: set[str]) -> dict[str, dict[str, Any]]:
    return {
        name: asyncio.run(
            ev.run_experiment(
                kb,
                name,
                ev.Override(
                    disable_hybrid=ev.EXPERIMENT_SPECS[name]["disable_hybrid"],
                    disable_embedding=ev.EXPERIMENT_SPECS[name]["disable_embedding"],
                ),
                queries,
                corpus_ids=corpus_ids,
                top_k=8,
                ks=(1, 3, 5, 8),
                timeout_s=30.0,
                warmup_n=0,
            )
        )
        for name in EXPERIMENT_NAMES
    }


def _producer_run(kb, queries, corpus_ids: set[str], *, reranker_ok: bool = True) -> str:
    results = _run_all_legs(kb, queries, corpus_ids)
    pop = ev.population_counts(queries, corpus_ids)
    validity = assess_evidence_validity(
        ev.collect_evidence_validity_observations(
            results,
            subset_run=False,
            declared_queries=len(queries),
            executed_queries=len(queries),
            population_counts=pop,
            preflight_status="OK",
        )
    )
    return ev.derive_run_status(
        full_run=all(r["n_success"] == len(queries) for r in results.values()),
        subset_run=False,
        reranker_ok=reranker_ok,
        validity=validity,
    )


def test_producer_run_summary_records_request_errors() -> None:
    """n_error is the exception/timeout count — distinct from n_failed, which
    also counts MISS_ALL / LOW_RANK / GOLD_NOT_INDEXED diagnostics."""
    queries = _queries(2)
    kb = _DeadChannelKB(degraded=False, fail_queries=frozenset({queries[0]["query"]}))
    result = asyncio.run(
        ev.run_experiment(
            kb,
            "hybrid_no_rerank",
            ev.Override(),
            queries,
            corpus_ids={"doc_0", "doc_1", "doc_2"},
            top_k=8,
            ks=(1, 3, 5, 8),
            timeout_s=30.0,
            warmup_n=0,
        )
    )
    assert result["n_error"] == 1
    assert result["n_success"] == 1
    assert result["n_failed"] >= 1  # diagnostics + the exception row
    assert result["failure_counts"]["TIMEOUT"] == 1


def test_producer_refuses_to_self_certify_a_dead_channel_run() -> None:
    """Reverse test through the real producer path (run_experiment -> status)."""
    queries = _queries(N)
    kb = _DeadChannelKB(degraded=True)
    status = _producer_run(kb, queries, corpus_ids={"doc_0", "doc_1", "doc_2"})
    assert status == "NOT_VERIFIED"
    assert status != "VERIFIED_FULL"


def test_producer_refuses_to_self_certify_a_missing_gold_corpus() -> None:
    queries = _queries(N)
    kb = _DeadChannelKB(degraded=False)
    # corpus contains none of the gold documents (import never landed)
    status = _producer_run(kb, queries, corpus_ids=set())
    assert status == "NOT_VERIFIED"


def test_producer_certifies_a_nominal_full_run() -> None:
    queries = _queries(N)
    kb = _DeadChannelKB(degraded=False)
    status = _producer_run(kb, queries, corpus_ids={"doc_0", "doc_1", "doc_2"})
    assert status == "VERIFIED_FULL"


def test_producer_never_labels_a_subset_run_formal() -> None:
    queries = _queries(16)
    kb = _DeadChannelKB(degraded=False)
    results = _run_all_legs(kb, queries, corpus_ids={"doc_0", "doc_1", "doc_2"})
    validity = assess_evidence_validity(
        ev.collect_evidence_validity_observations(
            results,
            subset_run=True,
            declared_queries=N,
            executed_queries=16,
            population_counts=ev.population_counts(queries, {"doc_0", "doc_1", "doc_2"}),
            preflight_status="OK",
        )
    )
    status = ev.derive_run_status(
        full_run=False, subset_run=True, reranker_ok=True, validity=validity
    )
    assert status == "SUBSET_SMOKE"
    assert E01_SUBSET_RUN in _codes(validity)


def test_channel_observations_are_derived_from_execution_facts() -> None:
    """Channel facts come from trace/meta, never from the shape of the result."""
    rows = [
        {
            "vector_channel_used": True,
            "lexical_channel_used": False,
            "channel_stages": {
                "VECTOR": {"status": "executed", "candidate_out": 32},
                "BM25": {"status": "degraded", "candidate_out": 0},
                "RERANK": {"status": "executed", "candidate_out": 5},
            },
        }
    ]
    facts = ev.collect_channel_observations(rows, ("vector", "bm25", "rerank"))
    assert facts["vector"] == {
        "required": True,
        "used_count": 1,
        "executed_count": 1,
        "candidate_total": 32,
    }
    assert facts["bm25"]["used_count"] == 0
    assert facts["bm25"]["candidate_total"] == 0
    assert facts["rerank"]["used_count"] == 1  # no meta flag: an executed stage is the fact
    assert ev.collect_channel_observations(rows, ("vector",))["bm25"]["required"] is False


# ---------------------------------------------------------------- consumer


def _benchmark(root: Path, n: int = N) -> str:
    payload = json.dumps(
        {
            "metadata": {"total_queries": n, "version": "benchmark-v1"},
            "queries": [
                {"id": f"q{i}", "query": f"问题 {i}", "expected_doc_ids": ["doc_0"]}
                for i in range(n)
            ],
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    path = root / "tests" / "eval" / "rag_benchmark.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _artifact(
    root: Path,
    observations: dict[str, Any],
    *,
    sha: str,
    status: str = "VERIFIED_FULL",
    validity: dict[str, Any] | None = None,
    n: int = N,
) -> Path:
    block = validity if validity is not None else assess_evidence_validity(observations)
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": "run-20261003T204445Z",
        "timestamp": "2026-10-03T20:44:45+00:00",
        "status": status,
        "git_sha": "c4fac3934de0856949efd15481301237cf820f46+dirty",
        "benchmark": {
            "sha256": sha,
            "declared_queries": n,
            "actual_queries": n,
            "executed_queries": observations["executed_queries"],
        },
        "preflight": {"status": observations["preflight_status"], "blockers": []},
        "metrics": {
            name: {"hit@3": 0.0, "recall@3": 0.0, "mrr@3": 0.0}
            for name in observations["experiments"]
        },
        "evaluation_populations": {
            "primary_view": "all_queries",
            "counts": observations["population_counts"],
        },
        "run_summary": {
            name: {
                "n_total": leg["n_total"],
                "n_success": leg["n_success"],
                "n_failed": 0,
                "n_error": leg["n_error"],
                "n_degraded": leg["n_degraded"],
                "failure_counts": leg["failure_counts"],
                "wall_seconds": 1.0,
            }
            for name, leg in observations["experiments"].items()
        },
        "evidence_validity": block,
        "subset_run": observations["subset_run"],
    }
    out = root / "artifacts" / "evaluation" / "rag-649" / report["run_id"]
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


def _derive(root: Path) -> str:
    return rag_evidence_status.derive_rag_formal_status(root)["rag_formal_metrics_status"]


def test_headline_status_stays_not_verified_for_a_dead_channel_artifact(
    tmp_path: Path,
) -> None:
    """The #45 artifact, byte-for-byte in the dimensions that matter.

    It claims ``status: VERIFIED_FULL``, completed 649/649 requests and carries
    625 GOLD_NOT_INDEXED. The derived headline must be NOT_VERIFIED.
    """
    sha = _benchmark(tmp_path)
    observations = _observations(
        eligible=24,
        covered=0,
        legs={
            name: _leg(
                name,
                n_degraded=N,
                gold_not_indexed=625,
                channel_used=0,
                candidates=0,
            )
            for name in EXPERIMENT_NAMES
        },
    )
    artifact = _artifact(tmp_path, observations, sha=sha)
    report = json.loads(artifact.read_text(encoding="utf-8"))
    assert report["status"] == "VERIFIED_FULL"  # the producer's own claim
    assert _derive(tmp_path) == "NOT_VERIFIED"


def test_headline_status_is_verified_for_a_nominal_full_run(tmp_path: Path) -> None:
    sha = _benchmark(tmp_path)
    _artifact(tmp_path, _observations(), sha=sha)
    info = rag_evidence_status.derive_rag_formal_status(tmp_path)
    assert info["rag_formal_metrics_status"] == "VERIFIED"
    assert info["rag_formal_artifact_schema_version"] == REPORT_SCHEMA_VERSION


def test_headline_status_is_not_verified_for_a_subset_run(tmp_path: Path) -> None:
    sha = _benchmark(tmp_path)
    observations = _observations(subset_run=True, declared=N, executed=16, eligible=16, covered=16)
    _artifact(
        tmp_path,
        observations,
        sha=sha,
        status="SUBSET_SMOKE",
        n=N,
    )
    assert _derive(tmp_path) == "NOT_VERIFIED"


def test_legacy_artifact_without_validity_block_cannot_certify(tmp_path: Path) -> None:
    """A pre-fix artifact structurally satisfies the old contract; it must not
    be able to promote itself now (fail-closed on missing measurements)."""
    sha = _benchmark(tmp_path)
    observations = _observations()
    artifact = _artifact(tmp_path, observations, sha=sha)
    report = json.loads(artifact.read_text(encoding="utf-8"))
    del report["evidence_validity"]
    artifact.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    assert _derive(tmp_path) == "NOT_VERIFIED"


def test_tampered_validity_verdict_is_rejected(tmp_path: Path) -> None:
    """A hand-flipped ``verdict: VALID`` on broken observations must not count."""
    sha = _benchmark(tmp_path)
    observations = _observations(
        eligible=24,
        covered=0,
        legs={name: _leg(name, gold_not_indexed=625) for name in EXPERIMENT_NAMES},
    )
    tampered = assess_evidence_validity(observations)
    tampered["verdict"] = VERDICT_VALID
    tampered["reasons"] = []
    _artifact(tmp_path, observations, sha=sha, validity=tampered)
    assert _derive(tmp_path) == "NOT_VERIFIED"


def test_observations_edited_away_from_run_summary_are_rejected(tmp_path: Path) -> None:
    """Editing the observed block while leaving run_summary intact is detected."""
    sha = _benchmark(tmp_path)
    observations = _observations(
        legs={
            name: _leg(name, n_degraded=N, channel_used=0, candidates=0)
            for name in EXPERIMENT_NAMES
        }
    )
    artifact = _artifact(tmp_path, observations, sha=sha)
    report = json.loads(artifact.read_text(encoding="utf-8"))
    # make the observations look healthy while run_summary still says otherwise
    report["evidence_validity"]["observed"] = _observations()
    artifact.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    assert _derive(tmp_path) == "NOT_VERIFIED"


def test_observations_consistency_check_catches_each_divergence() -> None:
    observations = _observations()
    report = {
        "subset_run": False,
        "preflight": {"status": "OK"},
        "evaluation_populations": {"counts": observations["population_counts"]},
        "run_summary": {
            name: {
                "n_total": N,
                "n_success": N,
                "n_error": 0,
                "n_degraded": 0,
                "failure_counts": {"GOLD_NOT_INDEXED": 0},
            }
            for name in EXPERIMENT_NAMES
        },
    }
    assert observations_are_consistent(observations, report, N)
    bad_subset = dict(observations, subset_run=True)
    assert not observations_are_consistent(bad_subset, report, N)
    bad_preflight = dict(observations, preflight_status="PARTIAL")
    assert not observations_are_consistent(bad_preflight, report, N)
    tampered_leg = json.loads(json.dumps(observations))
    tampered_leg["experiments"]["vector_only"]["n_degraded"] = 1
    assert not observations_are_consistent(tampered_leg, report, N)
    weakened = json.loads(json.dumps(observations))
    weakened["experiments"]["hybrid_rerank"]["channels"].pop("rerank")
    assert not observations_are_consistent(weakened, report, N)
    lying = json.loads(json.dumps(observations))
    lying["experiments"]["hybrid_rerank"]["channels"]["rerank"]["required"] = False
    assert not observations_are_consistent(lying, report, N)


def test_real_checkout_has_no_promoted_formal_artifact() -> None:
    """Guards the shipped repo: the committed artifacts must not certify."""
    assert _derive(PROJECT_ROOT) == "NOT_VERIFIED"


@pytest.mark.parametrize(
    "status",
    ["VERIFIED_FULL_NO_RERANK", "SUBSET_SMOKE", "PARTIAL", "NOT_VERIFIED", "BLOCKED"],
)
def test_non_formal_status_labels_are_never_promoted(tmp_path: Path, status: str) -> None:
    sha = _benchmark(tmp_path)
    _artifact(tmp_path, _observations(), sha=sha, status=status)
    assert _derive(tmp_path) == "NOT_VERIFIED"
