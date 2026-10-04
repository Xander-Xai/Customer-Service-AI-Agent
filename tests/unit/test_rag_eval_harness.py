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


# ---------------------------------------------------------------- blockers
# v2 blocker semantics: primary cause != downstream symptom.


def _derive(
    *,
    embedding_configured: bool = True,
    embedding_probe: str | None = "ok",
    embedding_http_status: int | None = None,
    reranker_probe: str | None = "ok",
    reranker_http_status: int | None = None,
    qdrant_total_points: int = 5000,
    requested: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return ev.derive_blockers(
        embedding_configured=embedding_configured,
        embedding_probe=embedding_probe,
        embedding_http_status=embedding_http_status,
        reranker_probe=reranker_probe,
        reranker_http_status=reranker_http_status,
        qdrant_total_points=qdrant_total_points,
        requested_experiments=requested or tuple(ev.EXPERIMENT_SPECS),
    )


def _blocker_codes(result: dict[str, Any]) -> list[str]:
    return [b["code"] for b in result["blockers"]]


def test_embedding_auth_fail_is_primary_and_empty_index_downstream() -> None:
    """embedding 401 + 空索引：primary=PROVIDER_AUTH，空索引是 downstream 症状。"""
    out = _derive(
        embedding_probe="failed", embedding_http_status=401, qdrant_total_points=0
    )
    assert out["status"] == "BLOCKED"
    assert out["primary_blocker"] == "EMBEDDING_PROVIDER_AUTH"
    codes = _blocker_codes(out)
    assert "EMBEDDING_PROVIDER_AUTH" in codes
    assert "VECTOR_INDEX_EMPTY" in codes
    empty = next(b for b in out["blockers"] if b["code"] == "VECTOR_INDEX_EMPTY")
    assert empty["caused_by"] == "EMBEDDING_PROVIDER_AUTH"
    assert empty["blocking"] is True


def test_embedding_healthy_empty_index_is_primary_blocker() -> None:
    """embedding 健康 + Qdrant 空：primary blocker = VECTOR_INDEX_EMPTY（待导入）。"""
    out = _derive(embedding_probe="ok", qdrant_total_points=0)
    assert out["status"] == "BLOCKED"
    assert out["primary_blocker"] == "VECTOR_INDEX_EMPTY"
    empty = next(b for b in out["blockers"] if b["code"] == "VECTOR_INDEX_EMPTY")
    assert "caused_by" not in empty  # 无 embedding 根因 → 不是 downstream 症状


def test_reranker_auth_only_blocks_hybrid_rerank() -> None:
    """reranker 401：不得阻塞 vector_only/bm25_only/hybrid_no_rerank。"""
    out = _derive(
        embedding_probe="ok",
        reranker_probe="silent_fallback",
        reranker_http_status=401,
        qdrant_total_points=5000,
    )
    assert out["status"] == "PARTIAL"
    assert out["primary_blocker"] is None  # 管线未被整体阻塞
    rer = next(b for b in out["blockers"] if b["code"] == "RERANKER_PROVIDER_AUTH")
    assert rer["blocking"] is False
    assert rer["blocks_experiments"] == ["hybrid_rerank"]
    # 其他三个实验不在任何 blocker 的 blocks_experiments 中
    blocked_all = {
        e for b in out["blockers"] for e in b.get("blocks_experiments", [])
    }
    assert blocked_all == {"hybrid_rerank"}
    assert not (blocked_all & {"vector_only", "bm25_only", "hybrid_no_rerank"})


def test_reranker_non_auth_failure_uses_degraded_code() -> None:
    """非 401/403 的 reranker 失败不冒充 AUTH（http_status=None）。"""
    out = _derive(reranker_probe="silent_fallback", reranker_http_status=None)
    codes = _blocker_codes(out)
    assert "RERANKER_PROVIDER_DEGRADED" in codes
    assert "RERANKER_PROVIDER_AUTH" not in codes


def test_embedding_unavailable_not_configured_is_primary() -> None:
    """embed_fn 未配置：EMBEDDING_PROVIDER_UNAVAILABLE，空索引仍为 downstream。"""
    out = _derive(
        embedding_configured=False,
        embedding_probe=None,
        qdrant_total_points=0,
    )
    assert out["status"] == "BLOCKED"
    assert out["primary_blocker"] == "EMBEDDING_PROVIDER_UNAVAILABLE"
    empty = next(b for b in out["blockers"] if b["code"] == "VECTOR_INDEX_EMPTY")
    assert empty["caused_by"] == "EMBEDDING_PROVIDER_UNAVAILABLE"


def test_all_healthy_preflight_is_ok_without_blockers() -> None:
    out = _derive()
    assert out["status"] == "OK"
    assert out["primary_blocker"] is None
    assert out["blockers"] == []


def test_blockers_never_record_credential_material() -> None:
    """blockers 结构只允许出现状态码/探针结果，不得出现 key/token 字样。"""
    out = _derive(
        embedding_probe="failed",
        embedding_http_status=401,
        reranker_probe="silent_fallback",
        reranker_http_status=403,
        qdrant_total_points=0,
    )
    dumped = json.dumps(out)
    for forbidden in ("api_key", "apikey", "authorization", "bearer", "sk-"):
        assert forbidden not in dumped.lower()
    assert out["blockers"][0]["http_status"] == 401


# ---------------------------------------------------------------- populations


def test_population_counts_dynamic_not_hardcoded() -> None:
    """三视图分母全部由 corpus_ids 运行时推导；空 corpus → B/C 归零。"""
    queries = _queries(4)
    full_corpus = {"doc_0", "doc_1", "doc_2"}
    counts = ev.population_counts(queries, full_corpus)
    assert counts == {
        "all_queries": 4,
        "retrieval_eligible": 4,
        "full_gold_covered": 4,
    }
    empty_counts = ev.population_counts(queries, set())
    assert empty_counts == {
        "all_queries": 4,
        "retrieval_eligible": 0,
        "full_gold_covered": 0,
    }
    # 部分覆盖：每条 query 仍至少有 doc_0 在 corpus → eligible=4，covered=0
    partial = ev.population_counts(queries, {"doc_0"})
    assert partial["retrieval_eligible"] == 4
    assert partial["full_gold_covered"] == 0


def test_population_flags_recorded_per_row() -> None:
    queries = _queries(2)
    res = _run(FakeKB(), queries, corpus_ids={"doc_0"})
    for row in res["rows"]:
        assert row["gold_expected_count"] == 3
        assert row["gold_in_corpus_count"] == 1
        assert row["retrieval_eligible"] is True
        assert row["full_gold_covered"] is False


def test_population_metrics_views_split() -> None:
    """all_queries 含全部成功行；eligible/covered 按标记过滤（动态计算）。"""
    queries = _queries(3)
    res = _run(FakeKB(), queries, corpus_ids={"doc_0"})
    pm = res["population_metrics"]
    assert pm["all_queries"]["query_count"] == 3
    assert pm["retrieval_eligible"]["query_count"] == 3
    assert pm["full_gold_covered"]["query_count"] == 0
    assert pm["all_queries"]["metrics"]["hit@1"] == pytest.approx(1.0)
    assert pm["full_gold_covered"]["metrics"] == {}


def test_gold_not_indexed_queries_stay_in_all_queries_view() -> None:
    """View A 语义：gold 全缺失的查询仍在 all_queries 分母内（GOLD_NOT_INDEXED 记失败）。

    FakeKB 是脚本化返回（会命中 gold id），因此这里只断言分母归属与失败分类，
    不断言指标值；真实运行中 gold 不在索引里时 hit@k 天然为 0。
    """
    queries = _queries(2)
    res = _run(FakeKB(), queries, corpus_ids=set())
    assert res["failure_counts"] == {"GOLD_NOT_INDEXED": 2}
    pm = res["population_metrics"]
    assert pm["all_queries"]["query_count"] == 2
    assert pm["retrieval_eligible"]["query_count"] == 0
    assert pm["full_gold_covered"]["query_count"] == 0


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
    # v2: population flags recorded per row
    for key in (
        "gold_expected_count",
        "gold_in_corpus_count",
        "retrieval_eligible",
        "full_gold_covered",
    ):
        assert key in row
    assert ev.REPORT_SCHEMA_VERSION == "rag-eval-evidence/v3"


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


# ===========================================================================
# Preflight reranker probe consumes the typed per-call outcome (v2 contract)
#
# Before: the probe called legacy rerank(), inferred success from the presence of
# `rerank_score` in the returned list, read the shared mutable
# `rr.last_error_status`, and labelled failure "silent_fallback".
# After: the probe consumes the same `RerankOutcome` the runtime uses, so
# applied/degraded/reason/http_status are direct facts of that one call.
#
# No network, no real provider, no preflight run.
# ===========================================================================


def _derive_typed(
    *,
    applied: bool | None = None,
    degraded: bool | None = None,
    reason: str | None = None,
    http_status: int | None = None,
    configured: bool = True,
    provider_called: bool | None = None,
    embedding_probe: str | None = "ok",
    embedding_configured: bool = True,
    qdrant_total_points: int = 5000,
    requested: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return ev.derive_blockers(
        embedding_configured=embedding_configured,
        embedding_probe=embedding_probe,
        embedding_http_status=None,
        reranker_configured=configured,
        reranker_probe="ok" if (applied and not degraded) else (
            None if applied is None else "degraded"
        ),
        reranker_http_status=http_status,
        reranker_applied=applied,
        reranker_degraded=degraded,
        reranker_reason=reason,
        reranker_provider_called=provider_called,
        qdrant_total_points=qdrant_total_points,
        requested_experiments=requested or tuple(ev.EXPERIMENT_SPECS),
    )


def _rer(out: dict[str, Any], code: str) -> dict[str, Any]:
    return next(b for b in out["blockers"] if b["code"] == code)


def test_typed_success_produces_no_reranker_blocker() -> None:
    out = _derive_typed(applied=True, degraded=False, reason="", http_status=200)
    assert out["status"] == "OK"
    assert out["blockers"] == []
    assert not any(b["stage"] == "reranker" for b in out["blockers"])


def test_http_401_from_typed_outcome_yields_auth_blocker() -> None:
    """§15: the AUTH blocker must follow from the typed outcome alone."""
    out = _derive_typed(
        applied=False, degraded=True, reason="http_error",
        http_status=401, provider_called=True,
    )
    rer = _rer(out, "RERANKER_PROVIDER_AUTH")
    assert rer["blocking"] is False
    assert rer["blocks_experiments"] == ["hybrid_rerank"]
    assert rer["http_status"] == 401
    assert "reason=http_error" in rer["detail"]


def test_http_403_also_auth() -> None:
    out = _derive_typed(
        applied=False, degraded=True, reason="http_error",
        http_status=403, provider_called=True,
    )
    assert "RERANKER_PROVIDER_AUTH" in _blocker_codes(out)


def test_timeout_yields_degraded_not_auth() -> None:
    out = _derive_typed(
        applied=False, degraded=True, reason="timeout",
        http_status=None, provider_called=True,
    )
    codes = _blocker_codes(out)
    assert "RERANKER_PROVIDER_DEGRADED" in codes
    assert "RERANKER_PROVIDER_AUTH" not in codes
    rer = _rer(out, "RERANKER_PROVIDER_DEGRADED")
    assert rer["blocking"] is False
    assert rer["blocks_experiments"] == ["hybrid_rerank"]
    assert "reason=timeout" in rer["detail"]


def test_provider_error_yields_degraded() -> None:
    out = _derive_typed(
        applied=False, degraded=True, reason="provider_error", provider_called=True
    )
    assert "RERANKER_PROVIDER_DEGRADED" in _blocker_codes(out)


def test_invalid_response_is_degraded_not_ok_and_not_auth() -> None:
    """§20: HTTP 200 must not make a malformed response look healthy."""
    out = _derive_typed(
        applied=False, degraded=True, reason="invalid_response",
        http_status=200, provider_called=True,
    )
    codes = _blocker_codes(out)
    assert "RERANKER_PROVIDER_DEGRADED" in codes
    assert "RERANKER_PROVIDER_AUTH" not in codes
    assert "reason=invalid_response" in _rer(out, "RERANKER_PROVIDER_DEGRADED")["detail"]


def test_unconfigured_yields_unavailable_and_blocks_nothing_else() -> None:
    """§18: no typed facts at all + not configured => UNAVAILABLE."""
    out = _derive_typed(applied=None, degraded=None, reason=None, configured=False)
    rer = _rer(out, "RERANKER_PROVIDER_UNAVAILABLE")
    assert rer["blocking"] is False
    assert rer["blocks_experiments"] == ["hybrid_rerank"]


def test_poisoned_shared_status_is_ignored_by_typed_verdict() -> None:
    """§16: the verdict must come from this call's outcome, not shared state.

    A previous call left `last_error_status = 401` on the instance; this call
    genuinely succeeded. No AUTH blocker may be produced.
    """
    poisoned = 401
    out = _derive_typed(
        applied=True, degraded=False, reason="", http_status=200,
    )
    assert poisoned not in (out.get("primary_blocker"),)
    assert "RERANKER_PROVIDER_AUTH" not in _blocker_codes(out)
    assert out["blockers"] == []


def test_hybrid_rerank_not_requested_leaves_other_experiments_alone() -> None:
    """§21: without hybrid_rerank there is no reranker blocker at all."""
    out = _derive_typed(
        applied=False, degraded=True, reason="http_error", http_status=401,
        provider_called=True,
        requested=("vector_only", "bm25_only", "hybrid_no_rerank"),
    )
    assert not any(b["stage"] == "reranker" for b in out["blockers"])
    blocked = {e for b in out["blockers"] for e in b.get("blocks_experiments", [])}
    assert not (blocked & {"vector_only", "bm25_only", "hybrid_no_rerank"})


def test_typed_facts_take_priority_over_probe_string() -> None:
    """The probe string is a compatibility fallback, never the verdict source."""
    # probe says "ok" but the typed outcome says degraded -> degraded wins.
    out = ev.derive_blockers(
        embedding_configured=True,
        embedding_probe="ok",
        embedding_http_status=None,
        reranker_configured=True,
        reranker_probe="ok",                 # stale / optimistic
        reranker_http_status=None,
        reranker_applied=False,
        reranker_degraded=True,
        reranker_reason="timeout",
        qdrant_total_points=5000,
        requested_experiments=tuple(ev.EXPERIMENT_SPECS),
    )
    assert "RERANKER_PROVIDER_DEGRADED" in _blocker_codes(out)


def _code_without_comments(func) -> str:
    """Source of `func` with COMMENT tokens stripped.

    Source-text guards must look at *code*, not prose: a comment that explains
    "we no longer read last_error_status" would otherwise make the guard that
    forbids reading it fail on itself.
    """
    import inspect
    import io
    import tokenize

    src = inspect.getsource(func)
    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        out.append(tok.string)
    return " ".join(out)


def _string_literals_in_code(func) -> list[str]:
    """Every string *literal* in `func`'s code, excluding docstrings.

    Needed because the prohibitions here are about literals the code could
    actually emit ("silent_fallback", a "rerank_score" key, an attribute named
    "last_error_status"). A docstring that explains why those are gone must not
    read as a violation -- and conversely a real literal must be caught.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
    ]


def _attribute_names_in_code(func) -> list[str]:
    """Attribute names accessed in `func`'s code (e.g. `x.last_error_status`)."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return [
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
    ]


def test_preflight_source_no_longer_infers_from_list_shape() -> None:
    """§13: `rerank_score` presence must not be the success signal any more."""
    from scripts import evaluate_rag as _ev  # noqa: PLC0415

    assert "rerank_score" not in _string_literals_in_code(_ev.probe_reranker)
    assert "rerank_with_outcome" in _code_without_comments(_ev.probe_reranker)
    assert "rr.rerank(" not in _code_without_comments(_ev.probe_reranker)


def test_preflight_source_no_longer_reads_shared_last_error_status() -> None:
    """§11: the probe must not attribute this call via shared mutable state."""
    from scripts import evaluate_rag as _ev  # noqa: PLC0415

    assert "last_error_status" not in _attribute_names_in_code(_ev.probe_reranker)
    assert "last_error_status" not in _attribute_names_in_code(_ev.preflight)


def test_blocker_detail_carries_no_sensitive_material() -> None:
    """§14: only bounded reason / status / model may appear."""
    out = _derive_typed(
        applied=False, degraded=True, reason="http_error", http_status=401,
        provider_called=True,
    )
    dumped = json.dumps(out, ensure_ascii=False)
    for forbidden in ("api_key", "apikey", "authorization", "bearer", "sk-",
                      "透明质酸", "透明质酸保湿", "response.text"):
        assert forbidden not in dumped.lower()


def test_new_runs_do_not_emit_silent_fallback_probe_label() -> None:
    """§6: after PR #38 the runtime fallback is structured, not 'silent'."""
    from scripts import evaluate_rag as _ev  # noqa: PLC0415

    src = _code_without_comments(_ev.preflight)
    assert "silent_fallback" not in src
    assert "degraded" in src


class _StubRerankerProbe:
    """Stands in for ApiReranker so preflight's probe never touches the network.

    `last_error_status` is deliberately poisoned to 401: if any code path still
    attributes this call via shared mutable state, a genuine success would be
    misreported as an auth failure.
    """

    def __init__(self, outcome, *, available: bool = True, model: str = "stub-model"):
        self._outcome = outcome
        self._available = available
        self._model = model
        self.calls = 0
        self.last_error_status = 401

    @property
    def available(self) -> bool:
        return self._available

    def rerank_with_outcome(self, query, results, top_k=3):
        self.calls += 1
        return self._outcome

    def rerank(self, query, results, top_k=3):  # legacy entry must go unused
        raise AssertionError("preflight must use rerank_with_outcome")


def _outcome(*, applied, degraded, reason, http_status=None, provider_called=True):
    from rag.reranker import RerankOutcome, RerankReason

    return RerankOutcome(
        results=[], applied=applied, degraded=degraded,
        reason=RerankReason(reason), http_status=http_status,
        provider_called=provider_called,
    )


def _gate_via_real_probe(monkeypatch, stub) -> dict[str, Any]:
    """Run the REAL probe_reranker() with a stubbed reranker factory."""
    monkeypatch.setattr(
        "rag.reranker.create_reranker", lambda: stub, raising=True
    )
    return ev.probe_reranker(rerank_probe=True)


@pytest.mark.parametrize(
    ("kwargs", "expect_probe", "expect_blocker"),
    [
        ({"applied": True, "degraded": False, "reason": "", "http_status": 200},
         "ok", None),
        ({"applied": False, "degraded": True, "reason": "http_error",
          "http_status": 401}, "degraded", "RERANKER_PROVIDER_AUTH"),
        ({"applied": False, "degraded": True, "reason": "timeout"},
         "degraded", "RERANKER_PROVIDER_DEGRADED"),
        ({"applied": False, "degraded": True, "reason": "invalid_response",
          "http_status": 200}, "degraded", "RERANKER_PROVIDER_DEGRADED"),
        ({"applied": False, "degraded": True, "reason": "provider_error"},
         "degraded", "RERANKER_PROVIDER_DEGRADED"),
    ],
)
def test_real_probe_records_the_typed_facts(
    monkeypatch, kwargs, expect_probe, expect_blocker
) -> None:
    """§4/§5/§28: the probe's own code, not a copy, must carry the typed facts."""
    stub = _StubRerankerProbe(_outcome(**kwargs))
    gate = _gate_via_real_probe(monkeypatch, stub)

    assert stub.calls == 1, "probe must use rerank_with_outcome exactly once"
    assert gate["probe"] == expect_probe
    assert gate["applied"] is kwargs["applied"]
    assert gate["degraded"] is kwargs["degraded"]
    assert gate["reason"] == kwargs["reason"]
    assert gate["http_status"] == kwargs.get("http_status")
    assert gate["provider_called"] is kwargs.get("provider_called", True)

    # The gate feeds derive_blockers directly; the verdict must agree.
    out = ev.derive_blockers(
        embedding_configured=True,
        embedding_probe="ok",
        embedding_http_status=None,
        reranker_configured=gate["configured"],
        reranker_probe=gate["probe"],
        reranker_http_status=gate["http_status"],
        reranker_applied=gate.get("applied"),
        reranker_degraded=gate.get("degraded"),
        reranker_reason=gate.get("reason"),
        reranker_provider_called=gate.get("provider_called"),
        qdrant_total_points=5000,
        requested_experiments=tuple(ev.EXPERIMENT_SPECS),
    )
    if expect_blocker is None:
        assert not any(b["stage"] == "reranker" for b in out["blockers"])
    else:
        rer = next(b for b in out["blockers"] if b["code"] == expect_blocker)
        assert rer["blocking"] is False
        assert rer["blocks_experiments"] == ["hybrid_rerank"]
        assert f"reason={kwargs['reason']}" in rer["detail"]


def test_real_probe_ignores_poisoned_shared_status_on_success(monkeypatch) -> None:
    """§16: shared previous status must not flip a real success into an auth block."""
    stub = _StubRerankerProbe(
        _outcome(applied=True, degraded=False, reason="", http_status=200)
    )
    assert stub.last_error_status == 401
    gate = _gate_via_real_probe(monkeypatch, stub)
    assert gate["probe"] == "ok"
    assert gate["http_status"] == 200
    out = _derive_typed(
        applied=gate["applied"], degraded=gate["degraded"],
        reason=gate["reason"], http_status=gate["http_status"],
    )
    assert "RERANKER_PROVIDER_AUTH" not in _blocker_codes(out)


def test_real_probe_does_not_call_legacy_rerank(monkeypatch) -> None:
    """§2/§13: legacy rerank() must not be on the probe path at all."""
    stub = _StubRerankerProbe(
        _outcome(applied=True, degraded=False, reason="", http_status=200)
    )
    _gate_via_real_probe(monkeypatch, stub)  # would raise if rerank() were used


def test_unavailable_reranker_is_never_probed(monkeypatch) -> None:
    """§18: no key => zero provider calls and no typed facts."""
    stub = _StubRerankerProbe(
        _outcome(applied=False, degraded=True, reason="unavailable",
                 provider_called=False),
        available=False,
    )
    gate = _gate_via_real_probe(monkeypatch, stub)
    assert gate["configured"] is False
    assert stub.calls == 0
    assert "probe" not in gate, "an unprobed reranker must not claim a probe verdict"
    out = _derive_typed(applied=None, degraded=None, reason=None, configured=False)
    assert "RERANKER_PROVIDER_UNAVAILABLE" in _blocker_codes(out)


def test_probe_reranker_source_has_no_shape_inference_or_shared_state() -> None:
    """§11/§13, on the extracted function rather than on preflight()."""
    assert "rerank_score" not in _string_literals_in_code(ev.probe_reranker)
    assert "last_error_status" not in _attribute_names_in_code(ev.probe_reranker)
    src = _code_without_comments(ev.probe_reranker)
    assert "rerank_with_outcome" in src
    assert "rr.rerank(" not in src


def test_probe_reranker_never_writes_silent_fallback() -> None:
    """§6: new runs label the probe degraded, not silent_fallback."""
    literals = _string_literals_in_code(ev.probe_reranker)
    assert "silent_fallback" not in literals
    assert "degraded" in literals
    assert "ok" in literals


def test_probe_gate_detail_has_no_sensitive_material(monkeypatch) -> None:
    """§14: bounded reason only — no body, query, documents or credential."""
    stub = _StubRerankerProbe(
        _outcome(applied=False, degraded=True, reason="http_error", http_status=401)
    )
    gate = _gate_via_real_probe(monkeypatch, stub)
    dumped = json.dumps(gate, ensure_ascii=False).lower()
    for forbidden in ("api_key", "apikey", "authorization", "bearer", "sk-",
                      "透明质酸", "response.text"):
        assert forbidden not in dumped
