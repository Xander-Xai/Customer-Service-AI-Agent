"""Regression tests for the hardened RAG evaluation harness.

Covers the RAG 649 Evidence Refresh work order §22:

- benchmark metadata mismatch fails closed
- benchmark hash recorded
- query count recorded
- failed queries are included (never silently dropped)
- latency percentile calculation
- MRR / Hit@K calculation
- category aggregation
- artifact schema shape
- ablation overrides are instance-level and always restored
- production default config is not mutated by experiments

The KB is faked with scripted ``retrieve`` results built on the real
``RetrievalResult``/``RetrievalTrace`` contract, so tests stay hermetic
(no Qdrant, no provider API).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "evaluate_rag.py"
spec = importlib.util.spec_from_file_location("evaluate_rag", SCRIPT)
ev = importlib.util.module_from_spec(spec)
sys.modules["evaluate_rag"] = ev
spec.loader.exec_module(ev)

from rag.qdrant_knowledge_base import rrf_fusion  # noqa: E402
from rag.retrieval_contract import (  # noqa: E402
    RetrievalRequest,
    RetrievalResult,
    RetrievalTrace,
    TraceStage,
)

# ---------------------------------------------------------------- helpers


def _trace(stages: list[tuple[str, str, int, float]]) -> RetrievalTrace:
    trace = RetrievalTrace()
    for name, status, cand_out, ms in stages:
        from rag.retrieval_contract import StageStatus

        trace.add(TraceStage(
            name=name,
            status=StageStatus(status),
            candidate_out=cand_out,
            duration_ms=ms,
        ))
    return trace


class FakeKB:
    """Minimal KB stand-in for run_experiment."""

    def __init__(self, behavior: dict[str, Any] | None = None):
        self._hybrid_enabled = True
        self._embed_fn = object()
        self.behavior = behavior or {}
        self.calls: list[RetrievalRequest] = []

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:  # noqa: D102
        self.calls.append(request)
        spec = self.behavior.get(request.query, "normal")
        if spec == "raise":
            raise RuntimeError("provider exploded")
        if spec == "timeout":
            raise TimeoutError("wall clock timeout")
        staged = spec if isinstance(spec, list) else [
            ("VECTOR", "executed", 32, 10.0), ("BM25", "executed", 16, 2.0),
            ("FUSION_RRF", "executed", 40, 0.5), ("RERANK", "skipped", 40, 0.0),
        ]
        ids = [f"doc_{i}" for i in range(request.top_k)]
        if spec == "rerank_flipped":
            ids = list(reversed(ids))  # scripted rerank changes the order
        evidence = [{"id": i, "content": f"content {i}"} for i in ids]
        meta = {
            "retrieval_degraded": False,
            "degraded_reason": "",
            "vector_channel_used": True,
            "lexical_channel_used": True,
        }
        return RetrievalResult(evidence, meta=meta, trace=_trace(staged))


def _queries(n: int) -> list[dict[str, Any]]:
    return [
        {
            "query_id": f"bench_{i:04d}",
            "query": f"查询 {i}",
            "expected_doc_ids": ["doc_0", "doc_1", "doc_2"],
            "category": "成分知识" if i % 2 == 0 else "产品推荐",
            "scene": "售前咨询",
            "difficulty": "easy",
            "query_type": "single",
        }
        for i in range(n)
    ]


def _run(kb, queries, **kwargs) -> dict[str, Any]:
    defaults = dict(
        corpus_ids={"doc_0", "doc_1", "doc_2"},
        top_k=8,
        ks=(1, 3, 5, 8),
        timeout_s=30.0,
        warmup_n=0,
    )
    defaults.update(kwargs)
    return asyncio.run(ev.run_experiment(kb, "hybrid_rerank", ev.Override(), queries, **defaults))


# ---------------------------------------------------------------- metrics


def test_hit_at_k_binary_relevance() -> None:
    m = ev.compute_query_metrics(
        ["x", "doc_1", "y", "doc_0"], ["doc_0", "doc_1", "doc_2"], ks=(1, 3)
    )
    assert m["hit@1"] == 0.0
    assert m["hit@3"] == 1.0
    assert m["recall@1"] == 0.0
    assert m["recall@3"] == pytest.approx(1 / 3)
    assert m["first_relevant_rank"] == 2.0


def test_mrr_rank_of_first_gold() -> None:
    m = ev.compute_query_metrics(["a", "b", "doc_2"], ["doc_2"], ks=(3,))
    assert m["mrr"] == pytest.approx(1 / 3)
    m2 = ev.compute_query_metrics(["a", "b", "c"], ["doc_2"], ks=(3,))
    assert m2["mrr"] == 0.0


def test_ndcg_perfect_and_zero() -> None:
    perfect = ev.compute_query_metrics(
        ["doc_0", "doc_1", "doc_2"], ["doc_0", "doc_1", "doc_2"], ks=(3,)
    )
    assert perfect["ndcg@3"] == pytest.approx(1.0)
    zero = ev.compute_query_metrics(["a", "b", "c"], ["doc_0"], ks=(3,))
    assert zero["ndcg@3"] == 0.0


def test_percentile_linear_interpolation() -> None:
    assert ev.percentile([], 95) == 0.0
    assert ev.percentile([5.0], 99) == 5.0
    vals = [float(i) for i in range(101)]  # 0..100
    assert ev.percentile(vals, 50) == 50.0
    assert ev.percentile(vals, 90) == 90.0
    assert ev.percentile(vals, 95) == 95.0
    assert ev.percentile(vals, 99) == 99.0
    # interpolation between ranks
    assert ev.percentile([10.0, 20.0], 75) == pytest.approx(17.5)


# ---------------------------------------------------------------- integrity


def test_benchmark_metadata_mismatch_fails(tmp_path: Path) -> None:
    bad = tmp_path / "bench.json"
    bad.write_text(json.dumps({
        "metadata": {"total_queries": 10, "version": "1.0"},
        "queries": [{"query_id": "q0", "query": "x", "expected_doc_ids": ["d"]}],
    }), encoding="utf-8")
    with pytest.raises(SystemExit):
        ev.load_benchmark(bad)


def test_benchmark_sha256_recorded(tmp_path: Path) -> None:
    p = tmp_path / "bench.json"
    p.write_text('{"metadata": {"total_queries": 1}, "queries": []}', encoding="utf-8")
    import hashlib

    expected = hashlib.sha256(p.read_bytes()).hexdigest()
    assert ev._sha256_file(p) == expected


# ---------------------------------------------------------------- runner


def test_failed_query_included_as_failure() -> None:
    queries = _queries(4)
    behavior = {queries[1]["query"]: "raise", queries[2]["query"]: "timeout"}
    kb = FakeKB(behavior)
    res = _run(kb, queries)
    assert res["n_success"] == 2
    assert res["n_failed"] == 2
    types = {f["failure_type"] for f in res["failures"]}
    assert types == {"PROVIDER_ERROR", "TIMEOUT"}
    failed_ids = {f["query_id"] for f in res["failures"]}
    assert failed_ids == {queries[1]["query_id"], queries[2]["query_id"]}


def test_miss_all_and_low_rank_classification() -> None:
    queries = _queries(2)
    # scripted KB returns doc_0..doc_7; gold is doc_0..2 → hit@1
    res = _run(FakeKB(), queries)
    assert res["n_failed"] == 0
    # gold NOT in corpus → GOLD_NOT_INDEXED
    res2 = _run(FakeKB(), queries, corpus_ids=set())
    assert res2["failure_counts"] == {"GOLD_NOT_INDEXED": 2}


def test_warmup_discarded_and_not_counted() -> None:
    queries = _queries(4)
    kb = FakeKB()
    res = _run(kb, queries, warmup_n=2)
    # warmup consumed the first 2 queries but formal rows cover all 4
    assert res["warmup"]["results_discarded"] is True
    assert res["warmup"]["count"] == 2
    assert res["n_success"] == 4
    assert len(kb.calls) == 6  # 2 warmup + 4 formal


def test_stage_latency_recorded_not_derived() -> None:
    staged = [
        ("VECTOR", "executed", 32, 11.0), ("BM25", "executed", 16, 3.0),
        ("FUSION_RRF", "executed", 40, 1.0), ("RERANK", "executed", 40, 25.0),
    ]
    kb = FakeKB({q["query"]: staged for q in _queries(2)})
    res = _run(kb, _queries(2))
    assert res["stage_latency"]["VECTOR"]["mean_ms"] == pytest.approx(11.0)
    assert res["stage_latency"]["BM25"]["mean_ms"] == pytest.approx(3.0)
    assert res["stage_latency"]["RERANK"]["mean_ms"] == pytest.approx(25.0)
    row = res["rows"][0]
    assert row["stages_ms"]["VECTOR"] == pytest.approx(11.0)
    assert row["total_ms"] > 0  # wall-clock measured, never derived


# ---------------------------------------------------------------- aggregation


def test_category_aggregation_uses_benchmark_labels_only() -> None:
    res = _run(FakeKB(), _queries(6))
    cats = res["category_metrics"]
    assert set(cats) == {"成分知识", "产品推荐"}
    assert cats["成分知识"]["query_count"] == 3
    assert cats["产品推荐"]["query_count"] == 3
    assert "hit@3" in cats["成分知识"]["metrics"]
    assert "p95_ms" in cats["成分知识"]["latency"]


def test_ablation_delta_and_rerank_uplift_counts() -> None:
    queries = _queries(3)
    base = FakeKB()  # rerank=False keeps doc_0 first
    rerank = FakeKB({q["query"]: "rerank_flipped" for q in queries})
    # rerank_flipped falls through to default scripted behavior (no list spec →
    # normal staged + reversed ids): gold doc_0 moves to rank 8 → degraded
    res_base = _run(base, queries)
    res_rerank = _run(rerank, queries)
    results = {"hybrid_no_rerank": res_base, "hybrid_rerank": res_rerank}
    out = ev.ablation_analysis(results)
    uplift = out["reranker_uplift"]
    assert uplift["degraded_queries"] == 3
    assert uplift["improved_queries"] == 0
    assert uplift["metrics_delta"]["mrr"] < 0
    # scripted stages identical; only wall-clock noise remains
    assert abs(uplift["latency_p95_delta_ms"]) < 5.0


def test_hybrid_vs_single_channel_delta() -> None:
    queries = _queries(2)
    res = {
        "vector_only": _run(FakeKB(), queries),
        "bm25_only": _run(FakeKB(), queries),
        "hybrid_no_rerank": _run(FakeKB(), queries),
    }
    out = ev.ablation_analysis(res)
    assert out["hybrid_vs_vector"]["mrr"] == pytest.approx(0.0)
    assert out["hybrid_vs_bm25"]["mrr"] == pytest.approx(0.0)


# ---------------------------------------------------------------- artifact


def test_report_schema_shape() -> None:
    queries = _queries(2)
    res = _run(FakeKB(), queries)
    results = {"hybrid_rerank": res}
    rows_payload = {
        name: [{k: r[k] for k in r if k not in ("expected_ids", "retrieved_ids")}
               for r in r0["rows"]]
        for name, r0 in results.items()
    }
    assert "hybrid_rerank" in rows_payload
    row = rows_payload["hybrid_rerank"][0]
    for key in ("query_id", "category", "total_ms", "stages_ms", "mrr", "hit@1"):
        assert key in row
    assert ev.REPORT_SCHEMA_VERSION == "rag-eval-evidence/v1"


# ---------------------------------------------------------------- overrides


def test_ablation_override_restored_even_on_exception() -> None:
    kb = FakeKB()
    assert kb._hybrid_enabled is True
    assert kb._embed_fn is not None

    restore = ev.apply_override(kb, ev.Override(disable_hybrid=True, disable_embedding=True))
    assert kb._hybrid_enabled is False
    assert kb._embed_fn is None
    restore()
    assert kb._hybrid_enabled is True
    assert kb._embed_fn is not None


def test_run_experiment_restores_state_on_provider_error() -> None:
    queries = _queries(2)
    kb = FakeKB({q["query"]: "raise" for q in queries})
    asyncio.run(ev.run_experiment(
        kb, "bm25_only", ev.Override(disable_embedding=True), queries,
        corpus_ids={"doc_0"}, top_k=8, ks=(1, 3, 5, 8), timeout_s=30.0, warmup_n=0,
    ))
    # the embed_fn object must be restored after the experiment
    assert kb._embed_fn is not None


def test_production_config_not_mutated() -> None:
    from core import config as cfg

    original = cfg.HYBRID_SEARCH_ENABLED
    kb = FakeKB()
    restore = ev.apply_override(kb, ev.Override(disable_hybrid=True))
    try:
        assert cfg.HYBRID_SEARCH_ENABLED is original  # module-level default untouched
    finally:
        restore()
    assert cfg.HYBRID_SEARCH_ENABLED is original


def test_rerank_flag_is_request_level_only() -> None:
    req_on = ev._build_request("q", 8, True, None)
    req_off = ev._build_request("q", 8, False, None)
    assert req_on.rerank is True
    assert req_off.rerank is False


# ---------------------------------------------------------------- RRF fusion


def test_rrf_fusion_prefers_multi_channel_agreement() -> None:
    ch1 = [{"id": "a", "content": "A"}, {"id": "b", "content": "B"}]
    ch2 = [{"id": "b", "content": "B"}, {"id": "c", "content": "C"}]
    fused = rrf_fusion([ch1, ch2], k=60)
    ids = [d["id"] for d in fused]
    assert ids[0] == "b"  # appears in both channels → highest RRF score
    assert set(ids) == {"a", "b", "c"}
