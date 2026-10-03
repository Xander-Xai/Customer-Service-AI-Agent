"""
P1-01: RAG Pipeline Unification — retrieval contract tests.

These tests pin the unified retrieval contract RAG-1..RAG-12. They are written
BEFORE the unification: on the old architecture every target test FAILS —
there is no ``retrieve()`` entrypoint, no ``RetrievalRequest`` /
``RetrievalResult`` / ``RetrievalTrace``, prefetch short-circuits scene
filters, and agents call ``search`` / ``query`` / ``query_multiple``
directly. The contract module ``rag.retrieval_contract`` does not exist yet.

Contract being pinned (spec P1-01):

    RetrievalRequest → KnowledgeBase.retrieve
        → Rewrite → Scene/Metadata Filter → Vector + BM25 → RRF → Reranker
        → Evidence (+ RetrievalTrace)

Invariants:
  RAG-1  single canonical entrypoint (business agents only call retrieve)
  RAG-2  rewrite owned by the pipeline, one canonical rewritten query in trace
  RAG-3  scene filter applied by the pipeline (not bypassable by prefetch)
  RAG-4  dense channel consumes P0-05 (fail-closed, no fake vector)
  RAG-5  BM25 channel consumes P1-02 readiness (not bypassable)
  RAG-6  RRF fuses executed channels (single-channel = degraded)
  RAG-7  reranker runs at most once (no double rerank)
  RAG-8  prefetch = reusable computation, never final evidence / never bypasses
  RAG-9  no duplicate rewrite / embedding / dense / BM25 / RRF / rerank
  RAG-10 degraded result contract (4 embedding×BM25 combos) preserved
  RAG-11 collection + scene mapping preserved (no product_knowledge defaulting)
  RAG-12 trace completeness (every stage recorded EXECUTED/SKIPPED/DEGRADED)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from rag.bm25_retriever import BM25Retriever
from rag.embedding_status import REASON_EMBEDDING_UNAVAILABLE
from rag.qdrant_knowledge_base import _EMBEDDING_DIM, QdrantKnowledgeBase

# The unified contract module does not exist on the old architecture. These
# imports are the RED signal: collection errors until the contract is built.
from rag.retrieval_contract import (  # noqa: E402  # noqa: E402  # noqa: E402
    STAGE_BM25,
    STAGE_FILTER,
    STAGE_FINAL,
    STAGE_FUSION_RRF,
    STAGE_RERANK,
    STAGE_REWRITE,
    STAGE_VECTOR,
    RetrievalRequest,
    RetrievalResult,
    RetrievalTrace,
    StageStatus,
)

# ---------------------------------------------------------------------------
# helpers (adapted from test_p005_embedding_failclosed.py patterns)
# ---------------------------------------------------------------------------


def _make_kb(embed_fn=None, *, hybrid=True):
    """Build a QdrantKnowledgeBase backed by a mock Qdrant client.

    The mock client records ``query_points`` (the qdrant-client 1.18 dense
    API) so the unified retrieve() can be observed calling it.
    """
    with (
        patch("rag.qdrant_knowledge_base.QdrantClient") as mock_cls,
        patch.object(
            QdrantKnowledgeBase,
            "_create_embedding_function",
            return_value=embed_fn,
        ),
    ):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.get_collections.return_value = MagicMock(collections=[])
        mock_count = MagicMock(count=0)
        mock_client.count.return_value = mock_count
        # retrieve()'s dense channel uses query_points (1.18 API); default [].
        mock_client.query_points.return_value = MagicMock(points=[])
        kb = QdrantKnowledgeBase(host="localhost", port=6333)
        kb._hybrid_enabled = hybrid
        return kb, mock_client


def _seed_bm25(kb, collection, docs):
    """Publish a READY BM25 index (mirrors production rebuild)."""
    from rag.bm25_lifecycle import BM25IndexMeta, BM25Readiness

    if kb._bm25 is None or kb._bm25 is False:
        kb._bm25 = BM25Retriever()
    kb._bm25.add_documents(docs, collection=collection)
    kb._bm25_readiness = BM25Readiness.READY
    kb._bm25_version_counter = getattr(kb, "_bm25_version_counter", 0) + 1
    kb._bm25_meta = BM25IndexMeta(
        index_version=kb._bm25_version_counter,
        document_count=kb._bm25.total_documents(),
        last_build_at=0.0,
        source_snapshot={
            "collections": {collection: {"point_count": len(docs), "doc_count": len(docs)}},
            "mode": "test_direct_seed",
        },
        status=BM25Readiness.READY,
        reason="",
    )


def _point(doc_id, content, score=0.9, *, scene=None):
    """A Qdrant scored-point double for query_points().points results."""
    p = MagicMock()
    p.id = hash(doc_id) & 0x7FFFFFFF
    p.score = score
    payload = {"doc_id": doc_id, "content": content}
    if scene:
        payload["scene"] = scene
    p.payload = payload
    return p


def _embed_fn():
    fn = MagicMock()
    fn.encode.return_value = np.array([[0.1] * _EMBEDDING_DIM], dtype=np.float32)
    return fn


# ---------------------------------------------------------------------------
# RAG-1 / RAG-12 / RAG-2 / RAG-3: contract shape, trace, stage order
# ---------------------------------------------------------------------------


class TestRetrieveContractShape:
    @pytest.mark.asyncio
    async def test_retrieve_returns_retrieval_result_with_trace(self):
        """RAG-1/RAG-12: retrieve() returns a RetrievalResult carrying a trace."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["烟酰胺精华液功效"])
        mock_client.query_points.return_value = MagicMock(
            points=[_point("d1", "烟酰胺精华液功效")]
        )
        result = await kb.retrieve(
            RetrievalRequest(query="烟酰胺", collections=["product_knowledge"])
        )
        assert isinstance(result, RetrievalResult)
        assert isinstance(result.trace, RetrievalTrace)

    @pytest.mark.asyncio
    async def test_retrieval_trace_contains_all_stages(self):
        """RAG-12: a healthy hybrid request records all 7 stages."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["烟酰胺精华液"])
        mock_client.query_points.return_value = MagicMock(
            points=[_point("d1", "烟酰胺精华液")]
        )
        result = await kb.retrieve(
            RetrievalRequest(query="烟酰胺", collections=["product_knowledge"], scene="售前咨询")
        )
        names = [s.name for s in result.trace.stages]
        for stage in (
            STAGE_REWRITE, STAGE_FILTER, STAGE_VECTOR, STAGE_BM25,
            STAGE_FUSION_RRF, STAGE_RERANK, STAGE_FINAL,
        ):
            assert stage in names, f"trace missing stage {stage}: {names}"

    @pytest.mark.asyncio
    async def test_rewrite_bm25_dense_rrf_rerank_order(self):
        """RAG-2: stage execution order is Rewrite→Filter→Vector→BM25→RRF→Rerank→Final."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["烟酰胺精华液"])
        mock_client.query_points.return_value = MagicMock(
            points=[_point("d1", "烟酰胺精华液")]
        )
        result = await kb.retrieve(
            RetrievalRequest(query="烟酰胺", collections=["product_knowledge"])
        )
        executed = [s.name for s in result.trace.stages if s.status is StageStatus.EXECUTED]
        # Strict: every stage is recorded in the canonical order (EXECUTED or
        # SKIPPED — e.g. RERANK is SKIPPED when fusion yields ≤1 candidate,
        # which is still a recorded stage in the right position).
        positions = {s.name: i for i, s in enumerate(result.trace.stages)}
        assert positions[STAGE_REWRITE] < positions[STAGE_FILTER]
        assert positions[STAGE_FILTER] < positions[STAGE_VECTOR]
        assert positions[STAGE_VECTOR] < positions[STAGE_BM25]
        assert positions[STAGE_BM25] < positions[STAGE_FUSION_RRF]
        assert positions[STAGE_FUSION_RRF] < positions[STAGE_RERANK]
        assert positions[STAGE_RERANK] < positions[STAGE_FINAL]
        # The healthy-hybrid path executes every stage up to fusion; rerank
        # may skip only when fusion collapses to a single candidate.
        for stage in (STAGE_REWRITE, STAGE_FILTER, STAGE_VECTOR, STAGE_BM25,
                      STAGE_FUSION_RRF, STAGE_FINAL):
            assert stage in executed, f"{stage} not EXECUTED in healthy hybrid: {executed}"

    @pytest.mark.asyncio
    async def test_canonical_rewritten_query_visible_in_trace(self):
        """RAG-2: the canonical rewritten query is recorded in the trace."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["烟酰胺精华液"])
        mock_client.query_points.return_value = MagicMock(points=[_point("d1", "烟酰胺")])
        result = await kb.retrieve(
            RetrievalRequest(query="烟酰胺精华", collections=["product_knowledge"])
        )
        # Synonym expansion appends domain synonyms; canonical query differs.
        assert result.trace.rewritten_query
        assert "烟酰胺" in result.trace.rewritten_query


# ---------------------------------------------------------------------------
# RAG-3 / RAG-8: prefetch must not bypass scene/filter or produce final evidence
# ---------------------------------------------------------------------------


class TestPrefetchDoesNotBypassPolicy:
    @pytest.mark.asyncio
    async def test_prefetch_does_not_bypass_scene_filters(self):
        """RAG-3/RAG-8: even with a prefetched embedding, the scene filter is
        applied by retrieve() — prefetch is reusable computation, not evidence."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["售后文档"])
        # Prefetched embedding reused; scene must STILL reach the Qdrant filter.
        prefetched = [0.1] * _EMBEDDING_DIM
        mock_client.query_points.return_value = MagicMock(points=[])
        await kb.retrieve(
            RetrievalRequest(
                query="退货",
                collections=["product_knowledge"],
                scene="售后支持",
                prefetched_embedding=prefetched,
            )
        )
        # The dense call must have carried a scene filter (query_filter with scene).
        assert mock_client.query_points.called
        call_kwargs = mock_client.query_points.call_args.kwargs
        qfilter = call_kwargs.get("query_filter")
        assert qfilter is not None, "scene filter not applied with prefetched embedding"
        # The filter must mention the scene field.
        conditions = getattr(qfilter, "must", []) or []
        scene_fields = []
        for cond in conditions:
            key = getattr(cond, "key", None)
            if key == "scene":
                scene_fields.append(key)
        assert scene_fields, "scene field not present in Qdrant filter"

    @pytest.mark.asyncio
    async def test_prefetch_does_not_produce_final_evidence(self):
        """RAG-8: a prefetched embedding is NOT final evidence — retrieve() still
        runs the dense/BM25/RRF/rerank pipeline and returns its own evidence."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["烟酰胺 BM25 命中"])
        hit = _point("d1", "烟酰胺 dense 命中")
        mock_client.query_points.return_value = MagicMock(points=[hit])
        result = await kb.retrieve(
            RetrievalRequest(
                query="烟酰胺",
                collections=["product_knowledge"],
                prefetched_embedding=[0.1] * _EMBEDDING_DIM,
            )
        )
        # Evidence comes from the pipeline (dense hit present), not the prefetch.
        contents = [r.get("content", "") for r in result]
        assert "烟酰胺 dense 命中" in contents
        # The vector stage EXECUTED (it queried Qdrant), not SKIPPED-as-final.
        vec = result.trace.stage(STAGE_VECTOR)
        assert vec is not None
        assert vec.status is StageStatus.EXECUTED


# ---------------------------------------------------------------------------
# RAG-9: no duplicate work (rewrite / embedding / dense / rerank)
# ---------------------------------------------------------------------------


class TestNoDuplicateWork:
    @pytest.mark.asyncio
    async def test_no_duplicate_embedding_or_double_retrieval(self):
        """RAG-9: when a prefetched embedding is supplied, retrieve() must NOT
        call _embed_texts again (no duplicate embedding) and must query Qdrant
        exactly once per collection (no double dense retrieval)."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["烟酰胺"])
        mock_client.query_points.return_value = MagicMock(points=[_point("d1", "烟酰胺")])
        kb._embed_texts = MagicMock(wraps=kb._embed_texts)
        await kb.retrieve(
            RetrievalRequest(
                query="烟酰胺",
                collections=["product_knowledge"],
                prefetched_embedding=[0.1] * _EMBEDDING_DIM,
            )
        )
        assert kb._embed_texts.call_count == 0, "prefetched embedding was not reused"
        assert mock_client.query_points.call_count == 1, "dense retrieval ran more than once"

    @pytest.mark.asyncio
    async def test_no_double_reranking(self):
        """RAG-7/RAG-9: the canonical reranker runs at most once per request."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["烟酰胺", "精华液", "功效"])
        mock_client.query_points.return_value = MagicMock(
            points=[_point("d1", "烟酰胺"), _point("d2", "精华液"), _point("d3", "功效")]
        )
        kb._apply_reranker = MagicMock(wraps=kb._apply_reranker)
        await kb.retrieve(
            RetrievalRequest(query="烟酰胺精华液", collections=["product_knowledge"], rerank=True)
        )
        assert kb._apply_reranker.call_count <= 1, "reranker ran more than once"


# ---------------------------------------------------------------------------
# RAG-11: collection + scene mapping preserved
# ---------------------------------------------------------------------------


class TestCollectionAndSceneMappingPreserved:
    @pytest.mark.asyncio
    async def test_collection_and_scene_mapping_are_preserved(self):
        """RAG-11: retrieve() queries the requested collections (not a defaulted
        product_knowledge) and applies the scene filter to each."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "complaint_knowledge", ["投诉文档"])
        mock_client.query_points.return_value = MagicMock(points=[])
        await kb.retrieve(
            RetrievalRequest(
                query="退款",
                collections=["complaint_knowledge"],
                scene="投诉处理",
            )
        )
        called_collections = {
            call.kwargs.get("collection_name") for call in mock_client.query_points.call_args_list
        }
        assert called_collections == {"complaint_knowledge"}, (
            "retrieve() did not preserve the requested collection mapping"
        )

    @pytest.mark.asyncio
    async def test_multiple_collections_all_queried(self):
        """RAG-11: a multi-collection request queries every collection."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "faq", ["faq 文档"])
        mock_client.query_points.return_value = MagicMock(points=[])
        await kb.retrieve(
            RetrievalRequest(
                query="问题",
                collections=["product_knowledge", "faq", "tech_support"],
            )
        )
        called = {
            call.kwargs.get("collection_name") for call in mock_client.query_points.call_args_list
        }
        assert called == {"product_knowledge", "faq", "tech_support"}


# ---------------------------------------------------------------------------
# RAG-4 / RAG-5 / RAG-10: degraded contract (P0-05 × P1-02 4 combos) + trace
# ---------------------------------------------------------------------------


class TestDegradedContract:
    @pytest.mark.asyncio
    async def test_dense_and_bm25_is_normal_hybrid(self):
        """RAG-10 combo A: embedding available + BM25 READY → non-degraded hybrid."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "c", ["烟酰胺"])
        mock_client.query_points.return_value = MagicMock(points=[_point("d1", "烟酰胺")])
        result = await kb.retrieve(RetrievalRequest(query="烟酰胺", collections=["c"]))
        meta = result.meta
        assert meta.get("retrieval_degraded") is False
        assert meta.get("vector_channel_used") is True
        assert meta.get("lexical_channel_used") is True

    @pytest.mark.asyncio
    async def test_dense_unavailable_bm25_ready_is_lexical_degraded(self):
        """RAG-10 combo B: embedding down + BM25 ready → BM25-only degraded."""
        kb, mock_client = _make_kb(embed_fn=None)
        _seed_bm25(kb, "c", ["烟酰胺精华液"])
        result = await kb.retrieve(RetrievalRequest(query="烟酰胺", collections=["c"]))
        meta = result.meta
        assert meta.get("retrieval_degraded") is True
        assert meta.get("degraded_reason") == "embedding_unavailable"
        assert meta.get("vector_channel_used") is False
        assert meta.get("lexical_channel_used") is True
        assert mock_client.query_points.call_count == 0  # no fake-vector query

    @pytest.mark.asyncio
    async def test_dense_available_bm25_not_ready_is_vector_degraded(self):
        """RAG-10 combo C: embedding up + BM25 not READY → vector-only degraded."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        # BM25 left UNINITIALIZED (not seeded) → not READY
        mock_client.query_points.return_value = MagicMock(points=[_point("d1", "烟酰胺")])
        result = await kb.retrieve(RetrievalRequest(query="烟酰胺", collections=["c"]))
        meta = result.meta
        assert meta.get("retrieval_degraded") is True
        assert meta.get("degraded_reason") == "bm25_not_ready"
        assert meta.get("vector_channel_used") is True
        assert meta.get("lexical_channel_used") is False

    @pytest.mark.asyncio
    async def test_neither_channel_is_no_retrieval_channel(self):
        """RAG-10 combo D: embedding down + BM25 not ready → no_retrieval_channel."""
        kb, _ = _make_kb(embed_fn=None)
        # BM25 not seeded → not READY
        result = await kb.retrieve(RetrievalRequest(query="q", collections=["c"]))
        meta = result.meta
        assert meta.get("retrieval_degraded") is True
        assert meta.get("degraded_reason") == "no_retrieval_channel"
        assert meta.get("vector_channel_used") is False
        assert meta.get("lexical_channel_used") is False

    @pytest.mark.asyncio
    async def test_bm25_not_ready_is_visible_in_retrieval_trace(self):
        """RAG-5/RAG-12: BM25 not ready → BM25 stage DEGRADED, visible in trace."""
        kb, _ = _make_kb(embed_fn=_embed_fn())
        # BM25 not seeded
        result = await kb.retrieve(RetrievalRequest(query="q", collections=["c"]))
        bm25_stage = result.trace.stage(STAGE_BM25)
        assert bm25_stage is not None
        assert bm25_stage.status is StageStatus.DEGRADED
        assert "bm25" in bm25_stage.reason or bm25_stage.reason == "bm25_not_ready"

    @pytest.mark.asyncio
    async def test_embedding_failure_is_visible_in_retrieval_trace(self):
        """RAG-4/RAG-12: embedding unavailable → VECTOR stage DEGRADED, visible."""
        kb, _ = _make_kb(embed_fn=None)
        _seed_bm25(kb, "c", [" lexical 命中 "])
        result = await kb.retrieve(RetrievalRequest(query="q", collections=["c"]))
        vec_stage = result.trace.stage(STAGE_VECTOR)
        assert vec_stage is not None
        assert vec_stage.status is StageStatus.DEGRADED
        assert "embedding" in vec_stage.reason

    @pytest.mark.asyncio
    async def test_retrieval_timeout_and_degraded_result_contract(self):
        """RAG-10/RAG-12: a retrieval-level timeout degrades gracefully (consume
        existing local timeout contract, NOT P1-06 global deadline). The dense
        channel timing out must mark VECTOR DEGRADED and not crash."""
        kb, mock_client = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "c", [" lexical 命中 "])

        def _hang(*a, **kw):
            raise TimeoutError("qdrant query_points timed out")

        mock_client.query_points.side_effect = _hang
        result = await kb.retrieve(
            RetrievalRequest(query="q", collections=["c"], retrieval_timeout=0.5)
        )
        # Does not crash; produces an explicit degraded result with a trace.
        assert isinstance(result, RetrievalResult)
        vec_stage = result.trace.stage(STAGE_VECTOR)
        assert vec_stage is not None
        assert vec_stage.status is StageStatus.DEGRADED


# ---------------------------------------------------------------------------
# RAG-1: business agents use the unified entrypoint (post-migration assertion)
# ---------------------------------------------------------------------------


class TestAgentsUseUnifiedEntrypoint:
    def test_retrieve_is_the_canonical_knowledge_base_method(self):
        """RAG-1: the KnowledgeBase exposes a ``retrieve`` coroutine."""
        assert hasattr(QdrantKnowledgeBase, "retrieve"), (
            "QdrantKnowledgeBase must expose a unified retrieve() entrypoint"
        )

    def test_old_retrieval_shims_are_not_used_by_business_agents(self):
        """RAG-1: business agents must NOT call search/query/query_multiple/
        simple_rerank/rewrite_query as their retrieval path. They route
        through _retrieve_knowledge → knowledge_base.retrieve. This inspects
        the BaseAgent retrieval method source so the migration cannot
        silently regress."""
        import inspect

        from agents.base_agent import BaseAgent

        src = inspect.getsource(BaseAgent._retrieve_knowledge)
        assert "retrieve(" in src, "BaseAgent._retrieve_knowledge does not call retrieve()"
        # The legacy direct KB orchestration calls must be gone (retrieve owns
        # rewrite + rerank; query/search/query_multiple are shims, not the
        # agent's path). ``query_multimodal`` (kept for the optional multimodal
        # hook) is intentionally not in this list — it is not text retrieval.
        for legacy in (
            ".search(",
            ".query_multiple(",
            ".simple_rerank(",
            ".rewrite_query(",
            "knowledge_base.query(",
        ):
            assert legacy not in src, (
                f"BaseAgent._retrieve_knowledge still calls the legacy shim {legacy}"
            )

    def test_all_business_agents_route_through_retrieve(self):
        """RAG-1: every business agent's retrieval goes through
        _retrieve_knowledge (which calls retrieve()), not direct KB calls."""
        import inspect

        from agents import (
            aftersales_agent,
            complaint_agent,
            product_agent,
            react_agent,
            sales_agent,
            tech_agent,
        )

        for mod in (complaint_agent, tech_agent, product_agent, aftersales_agent,
                    sales_agent, react_agent):
            src = inspect.getsource(mod)
            assert "_retrieve_knowledge" in src, f"{mod.__name__} does not use _retrieve_knowledge"
            # No direct knowledge_base.search/query/query_multiple calls.
            assert ".search(" not in src and ".query_multiple(" not in src, (
                f"{mod.__name__} calls the KB directly instead of _retrieve_knowledge"
            )


# ---------------------------------------------------------------------------
# Reranker truthfulness in the canonical pipeline
#
# The defect these pin: `RERANK` was recorded EXECUTED whenever reranking was
# *requested*, even when the provider failed and the pre-rerank ordering was
# handed back untouched. A caller could not tell real reranking from a
# plausible-looking fallback.
# ---------------------------------------------------------------------------


from rag.reranker import RerankOutcome, RerankReason  # noqa: E402


class _StubReranker:
    """Typed reranker double. Returns a real RerankOutcome, by construction."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = 0

    def rerank_with_outcome(self, query, results, top_k=3):
        self.calls += 1
        return self._outcome

    def rerank(self, query, results, top_k=3):  # legacy entry, must stay unused
        raise AssertionError("canonical pipeline must use rerank_with_outcome")


def _outcome(*, applied, degraded, reason, results=None):
    return RerankOutcome(
        results=results if results is not None else [],
        applied=applied,
        degraded=degraded,
        reason=RerankReason(reason),
        http_status=401 if reason == "http_error" else None,
        provider_called=reason != "unavailable",
    )


async def _retrieve_with(kb, mock_client, *, rerank: bool, outcome):
    _seed_bm25(kb, "product_knowledge", ["烟酰胺", "精华液", "功效"])
    mock_client.query_points.return_value = MagicMock(
        points=[_point("d1", "烟酰胺"), _point("d2", "精华液"), _point("d3", "功效")]
    )
    kb._reranker = _StubReranker(outcome)
    return await kb.retrieve(
        RetrievalRequest(
            query="烟酰胺精华液", collections=["product_knowledge"], rerank=rerank
        )
    )


def _rerank_stage(result):
    return result.trace.stage(STAGE_RERANK)


class TestRerankStageTruth:
    @pytest.mark.asyncio
    async def test_real_rerank_is_executed(self):
        kb, mc = _make_kb(embed_fn=_embed_fn())
        fused = [{"content": "d1", "id": "d1"}, {"content": "d2", "id": "d2"}]
        res = await _retrieve_with(
            kb, mc, rerank=True,
            outcome=_outcome(applied=True, degraded=False, reason="", results=fused),
        )
        stage = _rerank_stage(res)
        assert stage.status is StageStatus.EXECUTED
        assert stage.reason == ""
        assert res.meta["rerank_requested"] is True
        assert res.meta["rerank_applied"] is True
        assert res.meta["rerank_degraded"] is False
        assert res.meta["rerank_reason"] == ""
        assert res.meta["retrieval_degraded"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["unavailable", "timeout", "http_error",
                                        "provider_error", "invalid_response"])
    async def test_provider_failure_is_degraded_never_executed(self, reason):
        kb, mc = _make_kb(embed_fn=_embed_fn())
        fused = [{"content": "d1", "id": "d1"}, {"content": "d2", "id": "d2"}]
        res = await _retrieve_with(
            kb, mc, rerank=True,
            outcome=_outcome(applied=False, degraded=True, reason=reason, results=fused),
        )
        stage = _rerank_stage(res)
        assert stage.status is StageStatus.DEGRADED
        assert stage.reason == reason
        assert res.meta["rerank_applied"] is False
        assert res.meta["rerank_degraded"] is True
        assert res.meta["rerank_reason"] == reason
        # Availability preserved: real evidence still returned, not an error.
        assert len(res) > 0

    @pytest.mark.asyncio
    async def test_unavailable_failure_preserves_fused_order(self):
        kb, mc = _make_kb(embed_fn=_embed_fn())
        fused = [{"content": "first"}, {"content": "second"}]
        res = await _retrieve_with(
            kb, mc, rerank=True,
            outcome=_outcome(applied=False, degraded=True, reason="unavailable",
                             results=fused),
        )
        assert [e.get("content") for e in res] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_rerank_failure_promotes_overall_degradation(self):
        """Healthy channels + failed reranker is still a degraded result."""
        kb, mc = _make_kb(embed_fn=_embed_fn())
        res = await _retrieve_with(
            kb, mc, rerank=True,
            outcome=_outcome(applied=False, degraded=True, reason="timeout", results=[]),
        )
        assert res.meta["retrieval_degraded"] is True
        assert res.meta["degraded_reason"] == "reranker_timeout"

    @pytest.mark.asyncio
    async def test_http_status_is_exposed_as_a_code(self):
        kb, mc = _make_kb(embed_fn=_embed_fn())
        res = await _retrieve_with(
            kb, mc, rerank=True,
            outcome=_outcome(applied=False, degraded=True, reason="http_error", results=[]),
        )
        assert res.meta["reranker_http_status"] == 401

    @pytest.mark.asyncio
    async def test_rerank_disabled_is_skipped(self):
        kb, mc = _make_kb(embed_fn=_embed_fn())
        res = await _retrieve_with(
            kb, mc, rerank=False,
            outcome=_outcome(applied=False, degraded=False, reason="", results=[]),
        )
        stage = _rerank_stage(res)
        assert stage.status is StageStatus.SKIPPED
        assert stage.reason == "rerank_disabled"
        assert res.meta["rerank_requested"] is False
        assert res.meta["rerank_degraded"] is False
        assert res.meta["retrieval_degraded"] is False

    @pytest.mark.asyncio
    async def test_single_candidate_is_skipped_not_degraded(self):
        kb, mc = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["烟酰胺"])
        mc.query_points.return_value = MagicMock(points=[_point("d1", "烟酰胺")])
        stub = _StubReranker(_outcome(applied=False, degraded=False, reason="", results=[]))
        kb._reranker = stub
        res = await kb.retrieve(
            RetrievalRequest(query="烟酰胺", collections=["product_knowledge"], rerank=True)
        )
        stage = _rerank_stage(res)
        assert stage.status is StageStatus.SKIPPED
        assert stage.reason == "insufficient_candidates"
        assert stub.calls == 0, "reranker must not be called for one candidate"
        assert res.meta["rerank_degraded"] is False

    @pytest.mark.asyncio
    async def test_reranker_without_typed_contract_is_degraded(self):
        """An injected reranker that cannot prove a rerank is not reported as one."""
        kb, mc = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["烟酰胺", "精华液"])
        mc.query_points.return_value = MagicMock(
            points=[_point("d1", "烟酰胺"), _point("d2", "精华液")]
        )

        class _LegacyOnly:
            def rerank(self, query, results, top_k=3):
                return results[:top_k]

        kb._reranker = _LegacyOnly()
        res = await kb.retrieve(
            RetrievalRequest(query="烟酰胺", collections=["product_knowledge"], rerank=True)
        )
        stage = _rerank_stage(res)
        assert stage.status is StageStatus.DEGRADED
        assert res.meta["rerank_applied"] is False
        assert res.meta["rerank_degraded"] is True

    @pytest.mark.asyncio
    async def test_embedding_degradation_is_not_overwritten_by_rerank(self):
        """Both facts survive: channel root cause AND component degradation."""
        kb, mc = _make_kb(embed_fn=None)  # no embed_fn -> vector channel fails closed
        # Seed docs that all match the query so BM25 yields >1 candidate and the
        # RERANK stage is genuinely reached (1 candidate would be SKIPPED).
        _seed_bm25(kb, "product_knowledge", ["烟酰胺功效", "烟酰胺美白", "烟酰胺推荐"])
        kb._reranker = _StubReranker(
            _outcome(applied=False, degraded=True, reason="timeout", results=[])
        )
        res = await kb.retrieve(
            RetrievalRequest(query="烟酰胺", collections=["product_knowledge"], rerank=True)
        )
        assert _rerank_stage(res).status is StageStatus.DEGRADED
        meta = res.meta
        assert meta["retrieval_degraded"] is True
        # The channel root cause is still the headline reason...
        assert meta["degraded_reason"] == REASON_EMBEDDING_UNAVAILABLE
        # ...and the reranker fact is preserved alongside it.
        assert meta["rerank_degraded"] is True
        assert meta["rerank_reason"] == "timeout"

    @pytest.mark.asyncio
    async def test_degradation_propagates_to_agent_state(self):
        kb, mc = _make_kb(embed_fn=_embed_fn())
        _seed_bm25(kb, "product_knowledge", ["烟酰胺", "精华液", "功效"])
        mc.query_points.return_value = MagicMock(
            points=[_point("d1", "烟酰胺"), _point("d2", "精华液"), _point("d3", "功效")]
        )
        kb._reranker = _StubReranker(
            _outcome(applied=False, degraded=True, reason="http_error", results=[])
        )
        state: dict = {}
        await kb.retrieve(
            RetrievalRequest(
                query="烟酰胺精华液", collections=["product_knowledge"],
                rerank=True, state=state,
            )
        )
        assert state["retrieval_degraded"] is True
        assert state["degraded_reason"] == "reranker_http_error"


class TestRerankOutcomeIsNotDerivedFromSharedState:
    """The canonical outcome must come from this call, not from the instance.

    `last_error_status` is instance-level mutable state. With one reranker
    shared across concurrent requests, `call A -> rerank() -> switch ->
    call B writes last_error_status -> call A reads it` mis-attributes B's
    failure to A. So runtime attribution is pinned structurally, not just by a
    happy-path test.
    """

    def test_canonical_helpers_never_read_last_error_status(self):
        """Structural guarantee: no runtime decision reads the shared field."""
        import inspect

        from rag.qdrant_knowledge_base import QdrantKnowledgeBase

        for fn in (QdrantKnowledgeBase._apply_reranker_with_outcome,):
            src = inspect.getsource(fn)
            assert "last_error_status" not in src, (
                f"{fn.__name__} must derive its outcome from the returned value, "
                f"not from shared mutable state"
            )

    def test_retrieve_inner_rerank_block_never_reads_last_error_status(self):
        import inspect

        src = inspect.getsource(QdrantKnowledgeBase._retrieve_inner)
        rerank_block = src.split("# ---- 6. RERANK", 1)[1].split("# ---- 7. FINAL", 1)[0]
        assert "last_error_status" not in rerank_block

    @pytest.mark.asyncio
    async def test_interleaved_failure_does_not_poison_another_request(self):
        """Request A succeeds; B then fails; A's own meta must stay truthful."""
        kb, mc = _make_kb(embed_fn=_embed_fn())

        class _Interleaving:
            def __init__(self):
                self.n = 0
                self.last_error_status = None

            def rerank_with_outcome(self, query, results, top_k=3):
                self.n += 1
                if self.n == 2:
                    # Second request fails and writes the shared field.
                    self.last_error_status = 401
                    return RerankOutcome(results=results[:top_k], applied=False,
                                         degraded=True, reason=RerankReason.TIMEOUT)
                return RerankOutcome(results=results[:top_k], applied=True,
                                     degraded=False, reason=RerankReason.OK)

        kb._reranker = _Interleaving()
        _seed_bm25(kb, "product_knowledge", ["烟酰胺功效", "烟酰胺美白", "烟酰胺推荐"])
        mc.query_points.return_value = MagicMock(
            points=[_point("d1", "烟酰胺功效"), _point("d2", "烟酰胺美白"),
                    _point("d3", "烟酰胺推荐")]
        )

        first = await kb.retrieve(
            RetrievalRequest(query="烟酰胺", collections=["product_knowledge"], rerank=True)
        )
        # A concurrent failure happens after A completed.
        kb._reranker.last_error_status = 401
        second = await kb.retrieve(
            RetrievalRequest(query="烟酰胺", collections=["product_knowledge"], rerank=True)
        )

        assert first.meta["rerank_applied"] is True
        assert first.meta["rerank_degraded"] is False
        assert first.trace.stage(STAGE_RERANK).status is StageStatus.EXECUTED

        assert second.meta["rerank_applied"] is False
        assert second.meta["rerank_degraded"] is True
        assert second.meta["rerank_reason"] == "timeout"
        assert second.trace.stage(STAGE_RERANK).status is StageStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_stub_legacy_rerank_is_never_called_by_canonical_path(self):
        """The typed entry must be the one the pipeline uses."""
        kb, mc = _make_kb(embed_fn=_embed_fn())
        stub = _StubReranker(
            _outcome(applied=True, degraded=False, reason="", results=[])
        )
        kb._reranker = stub
        _seed_bm25(kb, "product_knowledge", ["烟酰胺功效", "烟酰胺美白", "烟酰胺推荐"])
        mc.query_points.return_value = MagicMock(
            points=[_point("d1", "烟酰胺功效"), _point("d2", "烟酰胺美白"),
                    _point("d3", "烟酰胺推荐")]
        )
        await kb.retrieve(
            RetrievalRequest(query="烟酰胺", collections=["product_knowledge"], rerank=True)
        )
        assert stub.calls == 1
