"""
P0-05: Random Embedding Fallback — fail-closed contract tests.

These tests pin the reliability invariants EMB-1..EMB-8: embedding dependency
failure must NEVER be coerced into a synthetic vector (random /
deterministic-random / hash-derived). The vector channel is explicitly
disabled, a BM25/lexical fallback runs only when ready, and a typed degraded
result with metadata + metrics reaches the caller.

Written BEFORE the fix. On the old implementation every target test FAILS
(old code emits random / deterministic-random vectors and queries Qdrant
with them).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from cache.response_cache import ResponseCache
from rag.bm25_retriever import BM25Retriever
from rag.embedding_status import (
    EmbeddingDimensionError,
    EmbeddingStatus,
    EmbeddingUnavailableError,
)
from rag.qdrant_knowledge_base import _EMBEDDING_DIM, QdrantKnowledgeBase

try:
    from prometheus_client import REGISTRY

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    PROMETHEUS_AVAILABLE = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_kb(embed_fn=None, *, hybrid=True):
    """Build a QdrantKnowledgeBase backed by a mock Qdrant client."""
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
        # query_with_vector / query / search call self._client.search; default to []
        mock_client.search.return_value = []
        kb = QdrantKnowledgeBase(host="localhost", port=6333)
        kb._hybrid_enabled = hybrid
        # deterministic point ids are fine for tests
        return kb, mock_client


def _seed_bm25(kb, collection, docs):
    """Seed the KB's BM25 lexical channel (embedding-independent)."""
    if kb._bm25 is None or kb._bm25 is False:
        kb._bm25 = BM25Retriever()
    kb._bm25.add_documents(docs, collection=collection)


def _counter_value(name: str) -> float:
    total = 0.0
    for metric in REGISTRY.collect():
        for s in metric.samples:
            if s.name == name:
                total += s.value
    return total


class _FakeQdrant:
    """Minimal Qdrant double recording query_points / upsert calls."""

    def __init__(self):
        self.collections: set[str] = set()
        self.points: dict[int, dict] = {}
        self.query_points_calls = 0
        self.upsert_calls = 0

    def get_collections(self):
        return MagicMock(collections=[MagicMock(name=n) for n in self.collections])

    def create_collection(self, collection_name, vectors_config=None):  # noqa: ARG002
        self.collections.add(collection_name)

    def upsert(self, collection_name, points):  # noqa: ARG002
        self.upsert_calls += 1
        for p in points:
            self.points[int(p.id)] = {"vector": list(p.vector), "payload": dict(p.payload)}

    def query_points(self, *a, **kw):  # noqa: ARG002
        self.query_points_calls += 1
        return MagicMock(points=[])


# ---------------------------------------------------------------------------
# EMB-1 / EMB-2: no synthetic vector; vector channel disabled
# ---------------------------------------------------------------------------


class TestNoSyntheticVector:
    def test_embedding_failure_never_generates_random_vector(self):
        """EMB-1: _embed_fn=None must NOT return a random vector — it must raise."""
        kb, _ = _make_kb(embed_fn=None)
        with pytest.raises(EmbeddingUnavailableError):
            kb._embed_texts(["any query"])

    def test_no_random_vector_is_emitted(self):
        """Repeated failures never emit a (random) vector — always a typed error."""
        kb, _ = _make_kb(embed_fn=None)
        for _ in range(3):
            with pytest.raises(EmbeddingUnavailableError):
                kb._embed_texts(["q"])

    def test_embedding_status_unavailable_when_no_embed_fn(self):
        kb, _ = _make_kb(embed_fn=None)
        assert kb.embedding_available is False
        assert kb.embedding_status() is EmbeddingStatus.UNAVAILABLE

    def test_embedding_status_available_when_embed_fn_present(self):
        mock_fn = MagicMock()
        mock_fn.encode.return_value = np.array([[0.1] * _EMBEDDING_DIM], dtype=np.float32)
        kb, _ = _make_kb(embed_fn=mock_fn)
        assert kb.embedding_available is True
        assert kb.embedding_status() is EmbeddingStatus.AVAILABLE


# ---------------------------------------------------------------------------
# EMB-2 / EMB-11: Qdrant is never queried with a fake vector
# ---------------------------------------------------------------------------


class TestNoFakeVectorQuery:
    @pytest.mark.asyncio
    async def test_embedding_failure_does_not_query_qdrant_with_fake_vector_query(self):
        """query() (vector-only) must not call Qdrant.search when embedding down."""
        kb, mock_client = _make_kb(embed_fn=None)
        result = await kb.query("c", "q")
        assert mock_client.search.call_count == 0
        # degraded empty result, not a silent []
        assert list(result) == []
        assert getattr(result, "meta", {}).get("retrieval_degraded") is True

    @pytest.mark.asyncio
    async def test_embedding_failure_does_not_query_qdrant_with_fake_vector_multiple(self):
        """query_multiple() must skip the vector channel entirely when embedding down."""
        kb, mock_client = _make_kb(embed_fn=None)
        _seed_bm25(kb, "c", ["烟酰胺精华液功效说明"])
        await kb.query_multiple(["c"], "烟酰胺")
        assert mock_client.search.call_count == 0


# ---------------------------------------------------------------------------
# EMB-3 / EMB-4 / EMB-7: controlled BM25 fallback, no-channel, degraded meta
# ---------------------------------------------------------------------------


class TestDegradedRetrieval:
    @pytest.mark.asyncio
    async def test_embedding_failure_degrades_to_bm25(self):
        """EMB-3: embedding down + BM25 ready → BM25-only results (no random noise)."""
        kb, mock_client = _make_kb(embed_fn=None)
        _seed_bm25(kb, "c", ["烟酰胺精华液可提亮肤色"])
        # Poison: if the vector channel ran, Qdrant would return this noise doc.
        noise = MagicMock()
        noise.id = 1
        noise.score = 0.99
        noise.payload = {"doc_id": "noise", "content": "RANDOM_VECTOR_NOISE_DOC"}
        mock_client.search.return_value = [noise]

        result = await kb.query_multiple(["c"], "烟酰胺", n_results=3)
        contents = [r.get("content", "") for r in result]
        assert "RANDOM_VECTOR_NOISE_DOC" not in contents
        assert any("烟酰胺" in c for c in contents)

    @pytest.mark.asyncio
    async def test_embedding_failure_returns_explicit_degraded_result(self):
        """Degraded result carries explicit metadata, not a silent list."""
        kb, _ = _make_kb(embed_fn=None)
        _seed_bm25(kb, "c", ["玻尿酸保湿"])
        result = await kb.query_multiple(["c"], "玻尿酸")
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is True
        assert meta.get("degraded_reason") == "embedding_unavailable"
        assert meta.get("vector_channel_used") is False
        assert meta.get("lexical_channel_used") is True

    @pytest.mark.asyncio
    async def test_retrieval_degraded_metadata(self):
        """Degraded metadata reaches the caller with provider/model info."""
        kb, _ = _make_kb(embed_fn=None)
        _seed_bm25(kb, "c", ["水杨酸"])
        result = await kb.query_multiple(["c"], "水杨酸")
        meta = getattr(result, "meta", {})
        for key in (
            "retrieval_degraded",
            "degraded_reason",
            "vector_channel_used",
            "lexical_channel_used",
            "embedding_provider",
            "embedding_model",
        ):
            assert key in meta, f"missing degraded meta key: {key}"
        assert isinstance(meta["embedding_model"], str)

    @pytest.mark.asyncio
    async def test_vector_and_cache_fail_closed_when_no_fallback(self):
        """EMB-4: no vector + no BM25 → explicit no-channel degraded result."""
        kb, _ = _make_kb(embed_fn=None)
        # BM25 empty (not seeded) → no lexical channel either
        result = await kb.query_multiple(["c"], "q")
        assert list(result) == []
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is True
        assert meta.get("degraded_reason") == "no_retrieval_channel"
        assert meta.get("vector_channel_used") is False
        assert meta.get("lexical_channel_used") is False

    @pytest.mark.asyncio
    async def test_normal_retrieval_not_marked_degraded(self):
        """Sanity: when embedding works, result is NOT flagged degraded."""
        mock_fn = MagicMock()
        mock_fn.encode.return_value = np.array([[0.1] * _EMBEDDING_DIM], dtype=np.float32)
        kb, mock_client = _make_kb(embed_fn=mock_fn)
        hit = MagicMock()
        hit.id = 1
        hit.score = 0.9
        hit.payload = {"doc_id": "d1", "content": "维C精华"}
        mock_client.search.return_value = [hit]
        result = await kb.query_multiple(["c"], "维C", n_results=3)
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is False


# ---------------------------------------------------------------------------
# EMB-6: dimension safety
# ---------------------------------------------------------------------------


class TestDimensionValidation:
    def test_embedding_dimension_mismatch_is_rejected(self):
        mock_fn = MagicMock()
        mock_fn.encode.return_value = np.array([[0.1] * 7], dtype=np.float32)  # wrong dim
        kb, _ = _make_kb(embed_fn=mock_fn)
        with pytest.raises(EmbeddingDimensionError):
            kb._embed_texts(["q"])

    def test_embedding_non_finite_is_rejected(self):
        mock_fn = MagicMock()
        bad = [float("nan")] * _EMBEDDING_DIM
        mock_fn.encode.return_value = np.array([bad], dtype=np.float32)
        kb, _ = _make_kb(embed_fn=mock_fn)
        with pytest.raises(EmbeddingDimensionError):
            kb._embed_texts(["q"])

    def test_embedding_empty_vector_is_rejected(self):
        mock_fn = MagicMock()
        mock_fn.encode.return_value = np.array([[]], dtype=np.float32)
        kb, _ = _make_kb(embed_fn=mock_fn)
        with pytest.raises(EmbeddingDimensionError):
            kb._embed_texts(["q"])

    def test_embedding_non_numeric_is_rejected(self):
        """Non-numeric elements are wrapped into the typed INVALID contract,
        not propagated as a bare ValueError."""
        mock_fn = MagicMock()
        # 1024-long vector of a non-numeric string — wrong-type element.
        mock_fn.encode.return_value = [["not-a-number"] * _EMBEDDING_DIM]
        kb, _ = _make_kb(embed_fn=mock_fn)
        with pytest.raises(EmbeddingDimensionError) as exc_info:
            kb._embed_texts(["q"])
        assert exc_info.value.reason == "non_numeric"

    def test_embedding_none_element_is_rejected(self):
        """None elements are rejected via the typed contract (TypeError wrapped)."""
        mock_fn = MagicMock()
        mock_fn.encode.return_value = [[None] * _EMBEDDING_DIM]
        kb, _ = _make_kb(embed_fn=mock_fn)
        with pytest.raises(EmbeddingDimensionError) as exc_info:
            kb._embed_texts(["q"])
        assert exc_info.value.reason == "non_numeric"


# ---------------------------------------------------------------------------
# Invalid vs unavailable: typed reason + status + caller metadata
# ---------------------------------------------------------------------------


class TestInvalidEmbeddingContract:
    def test_embedding_status_from_error_returns_invalid(self):
        from rag.embedding_status import EmbeddingStatus

        invalid = EmbeddingDimensionError(1024, 7, reason="dimension_mismatch")
        unavailable = EmbeddingUnavailableError(reason="provider_unavailable")
        assert EmbeddingStatus.from_error(invalid) is EmbeddingStatus.INVALID
        assert EmbeddingStatus.from_error(unavailable) is EmbeddingStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_invalid_embedding_metadata_says_embedding_invalid(self):
        """A wrong-dimension embedding must be reported as embedding_invalid in
        the degraded metadata, not mislabeled embedding_unavailable."""
        mock_fn = MagicMock()
        mock_fn.encode.return_value = np.array([[0.1] * 7], dtype=np.float32)  # wrong dim
        kb, _ = _make_kb(embed_fn=mock_fn)
        _seed_bm25(kb, "c", ["果酸功效"])
        res = await kb.query_multiple(["c"], "果酸", n_results=3)
        meta = getattr(res, "meta", {})
        assert meta.get("retrieval_degraded") is True
        assert meta.get("degraded_reason") == "embedding_invalid"
        assert meta.get("vector_channel_used") is False

    @pytest.mark.asyncio
    async def test_unavailable_embedding_metadata_says_embedding_unavailable(self):
        """No embed_fn is reported as embedding_unavailable (distinct from invalid)."""
        kb, _ = _make_kb(embed_fn=None)
        _seed_bm25(kb, "c", ["水杨酸"])
        res = await kb.query_multiple(["c"], "水杨酸", n_results=3)
        meta = getattr(res, "meta", {})
        assert meta.get("degraded_reason") == "embedding_unavailable"


# ---------------------------------------------------------------------------
# EMB-7: observable degradation (metric + log)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client 未安装")
class TestDegradedObservability:
    @pytest.mark.asyncio
    async def test_degraded_embedding_metric_and_log_are_emitted(self, caplog):
        # core.logger sets propagate=False; patch it True so records reach the
        # root logger where caplog's handler captures them.
        kb, _ = _make_kb(embed_fn=None)
        _seed_bm25(kb, "c", ["果酸"])
        before_vec = _counter_value("vector_channel_disabled_total")
        before_bm25 = _counter_value("bm25_fallback_used_total")

        kb_logger = logging.getLogger("rag.qdrant_knowledge_base")
        with patch.object(kb_logger, "propagate", True), caplog.at_level(logging.WARNING):
            await kb.query_multiple(["c"], "果酸")

        assert _counter_value("vector_channel_disabled_total") > before_vec
        assert _counter_value("bm25_fallback_used_total") > before_bm25
        msgs = [r.getMessage().lower() for r in caplog.records]
        assert any(
            "embedding" in m and ("unavailable" in m or "降级" in m) for m in msgs
        )


# ---------------------------------------------------------------------------
# EMB-8 / Step 14: benchmark honesty — degraded run not reported as normal vector
# ---------------------------------------------------------------------------


class TestBenchmarkHonesty:
    @pytest.mark.asyncio
    async def test_degraded_run_is_not_reported_as_normal_vector_run(self):
        """A degraded run's meta must not claim a normal vector/hybrid retrieval."""
        kb, _ = _make_kb(embed_fn=None)
        _seed_bm25(kb, "c", ["神经酰胺"])
        result = await kb.query_multiple(["c"], "神经酰胺")
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is True
        assert meta.get("vector_channel_used") is False
        # A benchmark reading this meta cannot truthfully label the run as a
        # normal vector/hybrid retrieval — it must be excluded or flagged.
        assert meta.get("degraded_reason") in {"embedding_unavailable", "no_retrieval_channel"}


# ---------------------------------------------------------------------------
# EMB-5: semantic cache fails closed (no deterministic-random vector)
# ---------------------------------------------------------------------------


class TestSemanticCacheFailClosed:
    def test_cache_embed_query_does_not_return_deterministic_random(self):
        """_embed_query must raise, not return random.Random(query) output."""
        cache = ResponseCache(qdrant_client=_FakeQdrant(), embedding_model=None)
        with pytest.raises(EmbeddingUnavailableError):
            cache._embed_query("q")

    def test_cache_embed_query_raises_on_runtime_encode_failure(self):
        mock_fn = MagicMock()
        mock_fn.encode.side_effect = Exception("api down")
        cache = ResponseCache(qdrant_client=_FakeQdrant(), embedding_model=mock_fn)
        with pytest.raises(EmbeddingUnavailableError):
            cache._embed_query("q")

    def test_cache_embed_query_rejects_nan(self):
        """A length-correct NaN vector must be rejected via the shared validator,
        not passed through to Qdrant."""
        mock_fn = MagicMock()
        mock_fn.encode.return_value = [float("nan")] * _EMBEDDING_DIM
        cache = ResponseCache(qdrant_client=_FakeQdrant(), embedding_model=mock_fn)
        with pytest.raises(EmbeddingDimensionError) as exc:
            cache._embed_query("q")
        assert exc.value.reason == "non_finite"

    def test_cache_embed_query_rejects_inf(self):
        mock_fn = MagicMock()
        mock_fn.encode.return_value = [float("inf")] * _EMBEDDING_DIM
        cache = ResponseCache(qdrant_client=_FakeQdrant(), embedding_model=mock_fn)
        with pytest.raises(EmbeddingDimensionError) as exc:
            cache._embed_query("q")
        assert exc.value.reason == "non_finite"

    def test_cache_embed_query_rejects_none_element(self):
        """A length-correct vector with None elements is rejected (non_numeric)."""
        mock_fn = MagicMock()
        mock_fn.encode.return_value = [None] * _EMBEDDING_DIM
        cache = ResponseCache(qdrant_client=_FakeQdrant(), embedding_model=mock_fn)
        with pytest.raises(EmbeddingDimensionError) as exc:
            cache._embed_query("q")
        assert exc.value.reason == "non_numeric"

    def test_cache_malformed_embedding_does_not_reach_qdrant(self):
        """EMB-5/EMB-6: a malformed (NaN) embedding must never reach Qdrant —
        no query_points on read, no upsert on write. P0-02 scope intact via L1/L3."""
        fake_q = _FakeQdrant()
        mock_fn = MagicMock()
        mock_fn.encode.return_value = [float("nan")] * _EMBEDDING_DIM
        cache = ResponseCache(
            qdrant_client=fake_q, embedding_model=mock_fn, fallback_enabled=False
        )
        cache.set("q", "resp", metadata={"intent_type": "knowledge_qa"})
        assert fake_q.upsert_calls == 0
        got = cache.get("q")
        assert fake_q.query_points_calls == 0
        assert got is None

    @pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client 未安装")
    def test_cache_malformed_embedding_increments_metric(self):
        """Malformed cache embedding enters the unified invalid monitoring counter."""
        mock_fn = MagicMock()
        mock_fn.encode.return_value = [float("nan")] * _EMBEDDING_DIM
        cache = ResponseCache(qdrant_client=_FakeQdrant(), embedding_model=mock_fn)
        before = _counter_value("semantic_cache_embedding_failures_total")
        with pytest.raises(EmbeddingDimensionError):
            cache._embed_query("q")
        assert _counter_value("semantic_cache_embedding_failures_total") > before

    def test_cache_embedding_failure_does_not_create_semantic_hit(self):
        """EMB-5: L2 skipped on embedding failure — no fake-vector semantic hit."""
        fake_q = _FakeQdrant()
        # L1 disabled (no redis), L3 disabled → only L2 active, so a hit can
        # ONLY come from the (now-skipped) semantic tier.
        cache = ResponseCache(
            qdrant_client=fake_q,
            embedding_model=None,
            fallback_enabled=False,
        )
        cache.set("q", "cached answer", metadata={"intent_type": "knowledge_qa"})
        # set must not have upserted a fake vector
        assert fake_q.upsert_calls == 0
        got = cache.get("q")
        # get must not have queried with a fake vector, and must miss
        assert fake_q.query_points_calls == 0
        assert got is None

    def test_cache_l2_skip_preserves_l1_exact_hit(self):
        """Skpping L2 must not break the L1 exact-match tier (P0-02 scope intact)."""
        fake_redis = MagicMock()
        import json

        fake_redis.get.return_value = json.dumps({"response": "l1 answer", "created_at": 0.0})
        cache = ResponseCache(
            redis_client=fake_redis,
            qdrant_client=_FakeQdrant(),
            embedding_model=None,
            fallback_enabled=False,
        )
        got = cache.get("q", metadata={"intent_type": "knowledge_qa"})
        assert got == "l1 answer"

    def test_cache_embedding_failure_does_not_break_scope_isolation(self):
        """P0-02: L2 skip must not let User A read User B's data via L1/L3."""
        fake_q = _FakeQdrant()
        cache = ResponseCache(
            qdrant_client=fake_q,
            embedding_model=None,
            fallback_enabled=False,
        )
        # User A writes a personal (non-public) answer.
        cache.set(
            "order 123",
            "User A secret order status",
            metadata={"intent_type": "order_status", "user_id": "userA"},
        )
        # User B reads the same query — must NOT get User A's answer.
        got = cache.get("order 123", metadata={"user_id": "userB"})
        assert got is None
        assert fake_q.upsert_calls == 0
        assert fake_q.query_points_calls == 0


# ---------------------------------------------------------------------------
# EMB-9 (ingestion): document embedding failure must not persist fake vectors
# ---------------------------------------------------------------------------


class TestIngestionFailClosed:
    def test_ingestion_does_not_persist_fake_vector_when_embedding_down(self):
        kb, mock_client = _make_kb(embed_fn=None, hybrid=False)
        kb.add_documents("c", ["doc text"])
        # No Qdrant upsert of random vectors.
        assert mock_client.upsert.call_count == 0

    def test_ingestion_still_indexes_bm25_when_embedding_down(self):
        """Lexical channel is embedding-independent and may still be indexed."""
        kb, _ = _make_kb(embed_fn=None, hybrid=True)
        kb.add_documents("c", ["烟酰胺文档"])
        assert kb._bm25 is not None and kb._bm25 is not False
        assert kb._bm25.collection_size("c") == 1


# ---------------------------------------------------------------------------
# Adversarial: runtime encode failure (embed_fn present, encode() raises)
# ---------------------------------------------------------------------------


class TestRuntimeEncodeFailure:
    @pytest.mark.asyncio
    async def test_kb_runtime_encode_failure_disables_vector_channel(self):
        """embed_fn present at pre-check but encode() raises at runtime → degrade.

        Adversarial scenario B: the typed EmbeddingUnavailableError must be
        caught by query_multiple (not crash, not fake a vector) and degrade to
        BM25-only when the lexical channel is ready.
        """
        mock_fn = MagicMock()
        mock_fn.encode.side_effect = Exception("embedding API timeout")
        kb, mock_client = _make_kb(embed_fn=mock_fn)
        _seed_bm25(kb, "c", ["烟酰胺精华"])
        # Poison: a successful vector channel would return this noise doc.
        noise = MagicMock()
        noise.id = 1
        noise.score = 0.99
        noise.payload = {"doc_id": "noise", "content": "RUNTIME_NOISE_DOC"}
        mock_client.search.return_value = [noise]

        result = await kb.query_multiple(["c"], "烟酰胺", n_results=3)
        # Vector channel disabled mid-flight — Qdrant never searched.
        assert mock_client.search.call_count == 0
        contents = [r.get("content", "") for r in result]
        assert "RUNTIME_NOISE_DOC" not in contents
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is True
        assert meta.get("vector_channel_used") is False
        assert meta.get("lexical_channel_used") is True

    @pytest.mark.asyncio
    async def test_repeated_failures_stay_fail_closed(self):
        """Adversarial scenario I: repeated failures never emit varying vectors."""
        kb, _ = _make_kb(embed_fn=None)
        _seed_bm25(kb, "c", ["果酸"])
        metas = []
        for _ in range(3):
            result = await kb.query_multiple(["c"], "果酸")
            metas.append(getattr(result, "meta", {}))
        # Every run is degraded; no run ever produced a (fake) vector.
        assert all(m.get("retrieval_degraded") is True for m in metas)
        assert all(m.get("vector_channel_used") is False for m in metas)
