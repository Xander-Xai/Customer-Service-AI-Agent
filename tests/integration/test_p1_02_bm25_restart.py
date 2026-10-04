"""P1-02: BM25 restart rebuild — real persistent Qdrant integration test.

Uses qdrant-client 1.18.0 **local path mode** (embedded Qdrant, no server) so
persistence across a "restart" is genuine: Instance A writes to a filesystem
path and closes; Instance B opens the SAME path with no seed/add_documents and
must recover BM25 from the persisted points.

This is the integration counterpart to the unit lifecycle tests in
``tests/unit/test_p1_02_bm25_lifecycle.py``. It proves BM25-1 (persistent
recovery), BM25-3 (no false hybrid claim), and that dense + BM25 recall both
survive a restart once the rebuild contract is implemented.

The vector channel in the test harness uses a ``search``→``query_points`` shim
because qdrant-client 1.18 dropped ``client.search()``. That shim is TEST-ONLY;
production vector code is unchanged (real-server compatibility is P1-01 scope).
"""

from __future__ import annotations

import shutil
import tempfile

import pytest

qdrant_client = pytest.importorskip("qdrant_client")
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.http import models  # noqa: E402

from rag.bm25_lifecycle import BM25Readiness  # noqa: E402
from rag.qdrant_knowledge_base import _EMBEDDING_DIM, QdrantKnowledgeBase  # noqa: E402

COLLECTION = "product_knowledge"
DOCS = [
    ("pr_000", "烟酰胺精华具有美白和控油功效 适合油性肌肤"),
    ("pr_001", "视黄醇抗衰老面霜能促进胶原蛋白生成 夜间使用"),
    ("pr_002", "水杨酸洁面乳深层清洁毛孔 适合痘痘肌肤"),
    ("pr_003", "透明质酸保湿精华强效补水 干性肌肤适用"),
]


def _deterministic_encode(texts):
    import hashlib

    out = []
    for t in texts:
        h = hashlib.sha256(t.encode("utf-8")).digest()
        vec = [(b / 255.0 - 0.5) for b in h[:_EMBEDDING_DIM]]
        vec = (vec * ((_EMBEDDING_DIM // len(vec)) + 1))[:_EMBEDDING_DIM]
        out.append(vec)
    return out


class _PathClient:
    """Wrap a path-mode QdrantClient and add a ``search`` shim (qdrant-client
    1.18 dropped ``client.search()`` in favour of ``query_points``). Delegates
    every other attribute to the real client so production code paths work."""

    def __init__(self, path: str):
        self._client = QdrantClient(path=path)

    def __getattr__(self, name):
        return getattr(self._client, name)

    def close(self):
        self._client.close()

    def search(self, collection_name, query_vector, limit=10, with_payload=True, **kw):  # noqa: ARG002
        r = self._client.query_points(
            collection_name, query=query_vector, limit=limit, with_payload=with_payload
        )
        return r.points


def _make_kb(path: str):
    """Build a QdrantKnowledgeBase backed by a real path-mode Qdrant client."""
    from unittest.mock import MagicMock, patch

    client = _PathClient(path)
    with (
        patch("rag.qdrant_knowledge_base.QdrantClient") as mock_cls,
        patch.object(QdrantKnowledgeBase, "_create_embedding_function", return_value=None),
    ):
        mock_cls.return_value = MagicMock()  # never used; we inject below
        kb = QdrantKnowledgeBase(host="localhost", port=6333)
    kb._client = client
    kb._available = True
    kb._collection_cache.clear()
    kb._hybrid_enabled = True
    kb._embed_fn = MagicMock()
    kb._embed_fn.encode = _deterministic_encode
    kb._embed_fn.model = "test-deterministic"
    kb._embedding_model_name = "test-deterministic"
    kb._embedding_provider = "TestEmbed"
    return kb, client


class _WorkingReranker:
    """Typed reranker double that reports a real rerank.

    `.env.test` leaves `RERANKER_API_KEY` empty (issue #52), so a real
    `ApiReranker` reports `available=False` and degrades the retrieval without
    calling any provider. That would make BM25/dense restart assertions depend
    on reranker configuration. Stubbing keeps this integration test about index
    persistence.
    """

    def rerank_with_outcome(self, query, results, top_k=3):
        from rag.reranker import RerankOutcome, RerankReason

        return RerankOutcome(
            results=results[:top_k], applied=True, degraded=False,
            reason=RerankReason.OK, provider_called=True,
        )


def _populate(kb, client):
    """Create the collection and add DOCS through the KB (Qdrant + BM25)."""
    client._client.create_collection(
        collection_name=COLLECTION,
        vectors_config=models.VectorParams(size=_EMBEDDING_DIM, distance=models.Distance.COSINE),
    )
    kb._collection_cache[COLLECTION] = True
    ids = [d[0] for d in DOCS]
    docs = [d[1] for d in DOCS]
    kb.add_documents(COLLECTION, docs, ids=ids)


@pytest.fixture
def persist_path():
    d = tempfile.mkdtemp(prefix="p1_02_it_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestRestartRebuild:
    def test_bm25_rebuilds_from_qdrant_after_restart(self, persist_path):
        """BM25-1: Instance A populates; Instance B opens the same persistent
        path with NO seed/add and recovers BM25 via rebuild."""
        # Instance A
        kbA, clientA = _make_kb(persist_path)
        _populate(kbA, clientA)
        assert kbA._ensure_bm25().collection_size(COLLECTION) == len(DOCS)
        clientA.close()
        del kbA

        # Instance B — fresh process, same persistent path, NO seed
        kbB, clientB = _make_kb(persist_path)
        assert kbB.get_collection_count(COLLECTION) == len(DOCS)  # Qdrant persisted
        # BM25 is empty before rebuild
        assert kbB.bm25_readiness() is BM25Readiness.UNINITIALIZED

        meta = kbB.rebuild_bm25_from_qdrant([COLLECTION])
        assert kbB.bm25_readiness() is BM25Readiness.READY
        assert meta.document_count == len(DOCS)
        # BM25 search recovered
        hits = kbB._bm25_search("烟酰胺 美白", [COLLECTION], top_k=3)
        assert len(hits) >= 1
        assert hits[0]["id"] == "pr_000"
        clientB.close()

    @pytest.mark.asyncio
    async def test_restart_preserves_dense_and_bm25_recall(self, persist_path):
        """After restart + rebuild, BOTH the dense (vector) channel and the
        BM25 (lexical) channel return relevant hits — hybrid is genuinely
        restored, not silently vector-only."""
        kbA, clientA = _make_kb(persist_path)
        _populate(kbA, clientA)
        clientA.close()
        del kbA

        kbB, clientB = _make_kb(persist_path)
        kbB._reranker = _WorkingReranker()
        kbB.rebuild_bm25_from_qdrant([COLLECTION])

        # Dense channel: vector search is functional after restart (returns
        # results). The fake embed_fn is a deterministic SHA-256 hash with no
        # real semantics, so we do NOT assert a specific doc id from the
        # vector channel — only that it is non-empty (the channel works).
        # The BM25 channel below carries the meaningful recall assertion.
        qvec = _deterministic_encode(["烟酰胺美白精华"])[0]
        dense = await kbB.query_with_vector(COLLECTION, qvec, n_results=3)
        assert len(dense) >= 1, f"dense channel returned nothing after restart: {dense}"

        # BM25 channel: lexical search must find pr_000 (rebuilt from persisted data)
        bm25_hits = kbB._bm25_search("烟酰胺 美白", [COLLECTION], top_k=3)
        bm25_ids = {h["id"] for h in bm25_hits}
        assert "pr_000" in bm25_ids, f"BM25 channel lost pr_000 after restart: {bm25_ids}"

        # Hybrid: query_multiple claims non-degraded with BOTH channels used
        result = await kbB.query_multiple([COLLECTION], "烟酰胺 美白", n_results=3)
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is False
        assert meta.get("vector_channel_used") is True
        assert meta.get("lexical_channel_used") is True
        clientB.close()

    @pytest.mark.asyncio
    async def test_restart_without_rebuild_is_degraded(self, persist_path):
        """BM25-3: after restart, if BM25 is NOT rebuilt, query_multiple must
        honestly mark degradation rather than silently going vector-only."""
        kbA, clientA = _make_kb(persist_path)
        _populate(kbA, clientA)
        clientA.close()
        del kbA

        kbB, clientB = _make_kb(persist_path)
        # deliberately do NOT rebuild
        result = await kbB.query_multiple([COLLECTION], "烟酰胺 美白", n_results=3)
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is True
        assert meta.get("degraded_reason") == "bm25_not_ready"
        assert meta.get("lexical_channel_used") is False
        clientB.close()
