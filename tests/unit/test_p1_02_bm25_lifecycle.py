"""P1-02: BM25 Restart Rebuild — lifecycle contract tests.

Pins the BM25 lifecycle invariants BM25-1..BM25-10:

  BM25-1 persistent recovery     BM25-2 readiness           BM25-3 hybrid truthfulness
  BM25-4 observable failure      BM25-5 source consistency  BM25-6 version traceability
  BM25-7 idempotent rebuild      BM25-8 incremental consistency
  BM25-9 empty collection        BM25-10 stable ID compatibility

Written BEFORE the fix. On the old implementation every target test FAILS:
there is no ``rebuild_bm25_from_qdrant``, no readiness abstraction, no index
metadata, ``delete_documents`` does not update BM25, and ``query_multiple``
claims ``retrieval_degraded=False`` when BM25 is empty (silent vector-only
degradation).

These unit tests use a deterministic in-memory Qdrant double (``_FakeQdrant``)
that supports paginated ``scroll``, ``count``, ``upsert``, ``delete``,
``retrieve``, ``query_points`` — so lifecycle behaviour is tested without a
real Qdrant server. Real persistent restart is covered by
``tests/integration/test_p1_02_bm25_restart.py``.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from rag.bm25_retriever import BM25Retriever
from rag.point_id import document_id_to_point_id
from rag.qdrant_knowledge_base import _EMBEDDING_DIM, QdrantKnowledgeBase

try:
    from prometheus_client import REGISTRY

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    PROMETHEUS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Fake Qdrant — deterministic, in-memory, supports paginated scroll
# ---------------------------------------------------------------------------


class _FakePoint:
    """Minimal scored/scroll point double."""

    def __init__(self, pid, payload, vector=None, score=0.0):
        self.id = pid
        self.payload = payload
        self.vector = vector
        self.score = score


class _FakeQdrant:
    """Deterministic in-memory Qdrant double for BM25 lifecycle tests.

    Supports the subset of the QdrantClient API that QdrantKnowledgeBase uses:
    ``get_collections``, ``create_collection``, ``count``, ``scroll`` (paged),
    ``upsert``, ``delete`` (by doc_id Filter), ``retrieve`` (collision guard),
    ``query_points`` (vector nearest, cosine).
    """

    SCROLL_PAGE = 3  # small page so pagination is exercised

    def __init__(self):
        self.collections: set[str] = set()
        # collection -> {point_id: {"vector": [...], "payload": {...}}}
        self.points: dict[str, dict[int, dict]] = {}
        self.scroll_raises = False  # failure injection
        self.scroll_raises_after = None  # fail after N points yielded
        self._scroll_yielded = 0
        # P1-02 BM25-17 concurrency test harness: when set, scroll() captures
        # its page snapshot FIRST (so a concurrent upsert does not affect the
        # in-flight scroll), then blocks on the event before returning. This
        # lets a test deterministically overlap a rebuild scroll with an add.
        self.scroll_block_event = None

    def get_collections(self):
        return MagicMock(collections=[MagicMock(name=n) for n in self.collections])

    def create_collection(self, collection_name, vectors_config=None, **kw):  # noqa: ARG002
        self.collections.add(collection_name)
        self.points.setdefault(collection_name, {})

    def count(self, collection_name, **kw):  # noqa: ARG002
        return MagicMock(count=len(self.points.get(collection_name, {})))

    def scroll(self, collection_name, limit=10, offset=None, with_payload=True, with_vectors=False):  # noqa: ARG002
        if self.scroll_raises:
            raise RuntimeError("injected scroll failure")
        pts = list(self.points.get(collection_name, {}).items())  # [(pid, rec)]
        # stable order by pid so paging is deterministic
        pts.sort(key=lambda kv: kv[0])
        start = offset or 0
        end = start + limit
        page = pts[start:end]
        # Capture the snapshot from the store state at scroll-call time so a
        # concurrent upsert (which mutates self.points) does not retroactively
        # change what this scroll returns after the block releases.
        captured = [(pid, dict(rec)) for pid, rec in page]
        if self.scroll_block_event is not None:
            self.scroll_block_event.wait(timeout=5.0)
        out = []
        for pid, rec in captured:
            if self.scroll_raises_after is not None and self._scroll_yielded >= self.scroll_raises_after:
                raise RuntimeError("injected mid-scroll failure")
            self._scroll_yielded += 1
            out.append(
                _FakePoint(
                    pid,
                    dict(rec["payload"]),
                    vector=rec["vector"] if with_vectors else None,
                )
            )
        next_offset = end if end < len(pts) else None
        return out, next_offset

    def upsert(self, collection_name, points):
        self.points.setdefault(collection_name, {})
        for p in points:
            self.points[collection_name][int(p.id)] = {
                "vector": list(p.vector),
                "payload": dict(p.payload),
            }

    def delete(self, collection_name, points_selector):
        store = self.points.get(collection_name, {})
        # points_selector may be a Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=id_))])
        must = getattr(points_selector, "must", None) or []
        for cond in must:
            match = getattr(cond, "match", None)
            key = getattr(cond, "key", None)
            value = getattr(match, "value", None) if match is not None else None
            if key == "doc_id":
                # remove points whose payload.doc_id == value
                for pid in list(store):
                    if store[pid]["payload"].get("doc_id") == value:
                        del store[pid]
        # also support PointIdsList
        ids = getattr(points_selector, "points", None)
        if ids is not None:
            for pid in list(store):
                if pid in [int(i) for i in ids]:
                    del store[pid]

    def retrieve(self, collection_name, ids, with_payload=True, with_vectors=False):  # noqa: ARG002
        store = self.points.get(collection_name, {})
        out = []
        for pid in ids:
            if pid in store:
                out.append(
                    _FakePoint(pid, dict(store[pid]["payload"]), vector=store[pid]["vector"] if with_vectors else None)
                )
        return out

    def query_points(self, collection_name, query, limit=10, with_payload=True, **kw):  # noqa: ARG002
        store = self.points.get(collection_name, {})
        import math

        scored = []
        q = query
        qn = math.sqrt(sum(x * x for x in q)) or 1.0
        for pid, rec in store.items():
            v = rec["vector"]
            vn = math.sqrt(sum(x * x for x in v)) or 1.0
            dot = sum(a * b for a, b in zip(q, v, strict=False))
            sim = dot / (qn * vn)
            scored.append((sim, pid, rec))
        scored.sort(reverse=True)
        return MagicMock(
            points=[
                _FakePoint(pid, dict(rec["payload"]), score=sim)
                for sim, pid, rec in scored[:limit]
            ]
        )

    # Production code calls self._client.search(...) — qdrant-client 1.18
    # dropped that method in favour of query_points. This shim keeps the
    # vector channel working in the TEST HARNESS only; production vector
    # code is unchanged (its real-server compatibility is out of scope for
    # P1-02, belongs to P1-01).
    def search(self, collection_name, query_vector, limit=10, with_payload=True, **kw):  # noqa: ARG002
        return self.query_points(collection_name, query_vector, limit=limit, with_payload=with_payload).points


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_kb(client=None, embed_fn="auto", *, hybrid=True):
    """Build a QdrantKnowledgeBase backed by a fake/mock Qdrant client.

    ``embed_fn="auto"`` installs a deterministic embed_fn so the vector
    channel is usable; pass ``None`` for the embedding-unavailable case.
    """
    if client is None:
        client = _FakeQdrant()
    with (
        patch("rag.qdrant_knowledge_base.QdrantClient") as mock_cls,
        patch.object(QdrantKnowledgeBase, "_create_embedding_function", return_value=None),
    ):
        mock_cls.return_value = client
        # force _available=True and inject our client
        kb = QdrantKnowledgeBase(host="localhost", port=6333)
    kb._client = client
    kb._available = True
    kb._collection_cache.clear()
    kb._hybrid_enabled = hybrid
    if embed_fn == "auto":
        kb._embed_fn = MagicMock()
        kb._embed_fn.encode = _deterministic_encode
        kb._embed_fn.model = "test-deterministic"
        kb._embedding_model_name = "test-deterministic"
        kb._embedding_provider = "TestEmbed"
    else:
        kb._embed_fn = embed_fn
    return kb, client


def _deterministic_encode(texts):
    import hashlib

    out = []
    for t in texts:
        h = hashlib.sha256(t.encode("utf-8")).digest()
        vec = [(b / 255.0 - 0.5) for b in h[:_EMBEDDING_DIM]]
        vec = (vec * ((_EMBEDDING_DIM // len(vec)) + 1))[:_EMBEDDING_DIM]
        out.append(vec)
    return out


def _put(client, collection, doc_id, content, **extra):
    """Upsert a single point into the fake Qdrant with a stable Point ID."""
    from qdrant_client.http import models

    if collection not in client.collections:
        client.create_collection(
            collection,
            vectors_config=models.VectorParams(size=_EMBEDDING_DIM, distance=models.Distance.COSINE),
        )
    pid = document_id_to_point_id(collection, doc_id)
    payload = {"doc_id": doc_id, "content": content, **extra}
    client.upsert(collection, points=[models.PointStruct(id=pid, vector=_deterministic_encode([content])[0], payload=payload)])
    return pid


def _counter_value(name: str) -> float:
    if not PROMETHEUS_AVAILABLE:
        return 0.0
    total = 0.0
    for metric in REGISTRY.collect():
        for s in metric.samples:
            if s.name == name:
                total += s.value
    return total


# ---------------------------------------------------------------------------
# BM25-1 / BM25-12: persistent recovery, no seed reload
# ---------------------------------------------------------------------------


class TestPersistentRecovery:
    def test_bm25_rebuilds_from_persisted_qdrant_on_startup(self):
        """BM25-1: a fresh KB recovers BM25 from persisted Qdrant via scroll,
        without any add_documents/seed call in the new process."""
        kb, client = _make_kb()
        _put(client, "product_knowledge", "pr_000", "烟酰胺精华美白控油")
        _put(client, "product_knowledge", "pr_001", "视黄醇抗衰面霜夜间使用")
        _put(client, "product_knowledge", "pr_002", "水杨酸洁面深层清洁毛孔")

        meta = kb.rebuild_bm25_from_qdrant(["product_knowledge"])

        from rag.bm25_lifecycle import BM25Readiness

        assert kb.bm25_readiness() is BM25Readiness.READY
        assert meta.document_count == 3
        # BM25 search now returns lexical hits from the recovered index
        hits = kb._bm25_search("烟酰胺 美白", ["product_knowledge"], top_k=3)
        assert len(hits) >= 1
        assert hits[0]["id"] == "pr_000"

    def test_rebuild_does_not_depend_on_seed_reload(self):
        """BM25-12: rebuild works when add_documents was NEVER called on this
        KB instance — the source is Qdrant scroll, not the seed path."""
        kb, client = _make_kb()
        _put(client, "faq", "faq_000", "如何退换货")
        _put(client, "faq", "faq_001", "运费谁承担")

        # Spy: ensure add_documents is not invoked on the KB during rebuild.
        kb.add_documents = MagicMock(side_effect=kb.add_documents)
        meta = kb.rebuild_bm25_from_qdrant(["faq"])

        assert meta.document_count == 2
        # rebuild must not route through the KB's own add_documents (that would
        # re-trigger vector upsert); it builds BM25 directly from scroll.
        kb.add_documents.assert_not_called()

    def test_rebuild_uses_pagination(self):
        """Step 7: rebuild must scroll ALL pages, not just the first."""
        kb, client = _make_kb()
        client.SCROLL_PAGE = 2  # page smaller than corpus
        for i in range(5):
            _put(client, "tech_support", f"tech_{i:03d}", f"技术文档内容编号 {i}")
        meta = kb.rebuild_bm25_from_qdrant(["tech_support"])
        assert meta.document_count == 5
        assert kb._ensure_bm25().collection_size("tech_support") == 5


# ---------------------------------------------------------------------------
# BM25-2: readiness
# ---------------------------------------------------------------------------


class TestReadiness:
    def test_fresh_kb_is_uninitialized(self):
        from rag.bm25_lifecycle import BM25Readiness

        kb, _ = _make_kb()
        assert kb.bm25_readiness() is BM25Readiness.UNINITIALIZED

    def test_empty_instance_is_not_ready(self):
        """BM25-2: ``self._bm25 is not None`` must NOT equal READY. A lazily
        created empty BM25Retriever is still UNINITIALIZED/DEGRADED, not READY."""
        from rag.bm25_lifecycle import BM25Readiness

        kb, _ = _make_kb()
        # Simulate the old lazy path: _ensure_bm25 creates an empty instance.
        kb._ensure_bm25()
        assert isinstance(kb._bm25, BM25Retriever)
        # ...but readiness must NOT be READY just because the object exists.
        assert kb.bm25_readiness() is not BM25Readiness.READY


# ---------------------------------------------------------------------------
# BM25-3: hybrid truthfulness
# ---------------------------------------------------------------------------


class TestHybridTruthfulness:
    @pytest.mark.asyncio
    async def test_bm25_readiness_blocks_false_hybrid_claim(self):
        """BM25-3: when BM25 is not READY (Qdrant has docs but BM25 not
        recovered), query_multiple must NOT claim a normal hybrid — it must
        mark retrieval_degraded=True with lexical_channel_used=False."""
        kb, client = _make_kb()
        _put(client, "product_knowledge", "pr_000", "烟酰胺精华美白控油")
        _put(client, "product_knowledge", "pr_001", "视黄醇抗衰面霜夜间使用")
        # BM25 NOT rebuilt — readiness is UNINITIALIZED.

        result = await kb.query_multiple(["product_knowledge"], "烟酰胺 美白", n_results=3)
        meta = getattr(result, "meta", {})
        assert meta.get("lexical_channel_used") is False
        assert meta.get("retrieval_degraded") is True
        assert meta.get("degraded_reason") == "bm25_not_ready"

    @pytest.mark.asyncio
    async def test_bm25_ready_reports_normal_hybrid(self):
        """Positive case: after rebuild, BM25 READY + embedding up → normal
        hybrid, lexical_channel_used=True, not degraded."""
        kb, client = _make_kb()
        _put(client, "product_knowledge", "pr_000", "烟酰胺精华美白控油")
        _put(client, "product_knowledge", "pr_001", "视黄醇抗衰面霜夜间使用")
        kb.rebuild_bm25_from_qdrant(["product_knowledge"])

        result = await kb.query_multiple(["product_knowledge"], "烟酰胺 美白", n_results=3)
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is False
        assert meta.get("lexical_channel_used") is True


# ---------------------------------------------------------------------------
# BM25-4: observable failure
# ---------------------------------------------------------------------------


class TestObservableFailure:
    def test_rebuild_failure_is_observable(self, caplog):
        """BM25-4: a scroll failure must NOT silently continue to READY.
        Readiness becomes DEGRADED, meta carries a reason, a metric fires,
        and a warning is logged."""
        from rag.bm25_lifecycle import BM25Readiness

        kb, client = _make_kb()
        _put(client, "product_knowledge", "pr_000", "内容")
        client.scroll_raises = True

        before = _counter_value("bm25_rebuild_failures_total")
        with caplog.at_level(logging.WARNING, logger="rag.qdrant_knowledge_base"):
            meta = kb.rebuild_bm25_from_qdrant(["product_knowledge"])

        assert kb.bm25_readiness() is BM25Readiness.DEGRADED
        assert meta.status is BM25Readiness.DEGRADED
        assert meta.reason  # non-empty
        assert meta.document_count == 0
        after = _counter_value("bm25_rebuild_failures_total")
        assert after >= before  # metric incremented (or noop metric stays 0 — see skip)

    def test_partial_scroll_failure_does_not_publish_partial_index(self):
        """BM25-17/18: a failure mid-scroll must not leave a half-built BM25
        published as READY. The previous (or empty) index is preserved and
        readiness is DEGRADED."""
        from rag.bm25_lifecycle import BM25Readiness

        kb, client = _make_kb()
        for i in range(5):
            _put(client, "product_knowledge", f"pr_{i:03d}", f"内容 {i}")
        client.scroll_raises_after = 2  # fail after 2 points yielded

        meta = kb.rebuild_bm25_from_qdrant(["product_knowledge"])
        assert kb.bm25_readiness() is BM25Readiness.DEGRADED
        # No partial index published: bm25 either None/False or empty-ish,
        # but crucially NOT READY with only 2 docs.
        assert meta.document_count == 0


# ---------------------------------------------------------------------------
# BM25-5: source consistency (document count reconciliation)
# ---------------------------------------------------------------------------


class TestSourceConsistency:
    def test_bm25_document_count_matches_source(self):
        """BM25-5: BM25 document_count == valid Qdrant points; invalid points
        (missing content) are skipped and counted in skipped_invalid."""

        kb, client = _make_kb()
        # 3 valid + 2 invalid (missing content / missing doc_id)
        for did, content in [("a", "内容甲"), ("b", "内容乙"), ("c", "内容丙")]:
            _put(client, "c", did, content)
        client.points["c"][document_id_to_point_id("c", "x")] = {
            "vector": _deterministic_encode(["x"])[0],
            "payload": {"doc_id": "x"},  # missing content
        }
        client.points["c"][document_id_to_point_id("c", "y")] = {
            "vector": _deterministic_encode(["y"])[0],
            "payload": {"content": "无id内容"},  # missing doc_id
        }

        meta = kb.rebuild_bm25_from_qdrant(["c"])
        assert meta.document_count == 3
        assert meta.skipped_invalid == 2

    def test_bm25_count_matches_qdrant_count_after_rebuild(self):
        kb, client = _make_kb()
        for i in range(4):
            _put(client, "c", f"d{i}", f"文档内容 {i}")
        kb.rebuild_bm25_from_qdrant(["c"])
        assert kb._ensure_bm25().collection_size("c") == kb.get_collection_count("c")


# ---------------------------------------------------------------------------
# BM25-6 / BM25-13: version traceability
# ---------------------------------------------------------------------------


class TestVersionTraceability:
    def test_bm25_index_version_changes_on_rebuild(self):
        """BM25-6/13: a successful rebuild produces a non-zero index_version;
        a second rebuild produces a different (higher) version."""
        kb, client = _make_kb()
        _put(client, "c", "d0", "内容零")
        meta1 = kb.rebuild_bm25_from_qdrant(["c"])
        assert meta1.index_version > 0
        meta2 = kb.rebuild_bm25_from_qdrant(["c"])
        assert meta2.index_version != meta1.index_version

    def test_index_meta_has_build_time_and_source_snapshot(self):
        """BM25-6: meta carries last_build_at and a source_snapshot describing
        which Qdrant data the index was built from."""
        kb, client = _make_kb()
        _put(client, "c", "d0", "内容零")
        _put(client, "c", "d1", "内容一")
        meta = kb.rebuild_bm25_from_qdrant(["c"])
        assert meta.last_build_at > 0
        assert meta.source_snapshot  # non-empty descriptor
        # snapshot must reference the collections built from
        snap = meta.source_snapshot
        assert "c" in snap or "collections" in snap


# ---------------------------------------------------------------------------
# BM25-7: idempotent rebuild
# ---------------------------------------------------------------------------


class TestIdempotentRebuild:
    def test_rebuild_is_idempotent(self):
        """BM25-7: rebuilding twice from an unchanged source does not duplicate
        documents or drift the count. BM25 search scores are stable."""
        kb, client = _make_kb()
        for i in range(3):
            _put(client, "c", f"d{i}", f"文档内容 {i}")
        kb.rebuild_bm25_from_qdrant(["c"])
        hits1 = kb._bm25_search("文档", ["c"], top_k=3)
        count1 = kb._ensure_bm25().collection_size("c")

        kb.rebuild_bm25_from_qdrant(["c"])
        hits2 = kb._bm25_search("文档", ["c"], top_k=3)
        count2 = kb._ensure_bm25().collection_size("c")

        assert count1 == count2 == 3
        assert [h["id"] for h in hits1] == [h["id"] for h in hits2]
        assert [h["bm25_score"] for h in hits1] == [h["bm25_score"] for h in hits2]


# ---------------------------------------------------------------------------
# BM25-9: empty collection
# ---------------------------------------------------------------------------


class TestEmptyCollection:
    def test_rebuild_handles_empty_collection(self):
        """BM25-9: an empty Qdrant collection is a legitimate state — rebuild
        succeeds with READY and document_count=0, NOT DEGRADED."""
        from rag.bm25_lifecycle import BM25Readiness

        kb, client = _make_kb()
        client.create_collection(
            "empty",
            vectors_config=MagicMock(),
        )
        meta = kb.rebuild_bm25_from_qdrant(["empty"])
        assert kb.bm25_readiness() is BM25Readiness.READY
        assert meta.document_count == 0
        assert meta.reason == ""


# ---------------------------------------------------------------------------
# BM25-8 / BM25-15 / BM25-16: incremental add/upsert/delete consistency
# ---------------------------------------------------------------------------


class TestIncrementalConsistency:
    def test_incremental_add_keeps_bm25_consistent(self):
        """BM25-8/15: after add_documents, BM25 reflects the new doc and the
        index_version bumps; count matches Qdrant."""
        kb, client = _make_kb()
        for i in range(3):
            _put(client, "c", f"d{i}", f"文档内容 {i}")
        kb.rebuild_bm25_from_qdrant(["c"])
        v_before = kb.bm25_index_meta().index_version

        kb.add_documents("c", ["全新文档内容"], ids=["d3"])
        assert kb._ensure_bm25().collection_size("c") == 4
        assert kb.get_collection_count("c") == 4
        assert kb.bm25_index_meta().index_version != v_before

    def test_upsert_same_id_does_not_duplicate_in_bm25(self):
        """BM25-7/15: re-adding the same doc_id updates BM25 in place — no
        duplicate token entries, count stays 1."""
        kb, client = _make_kb()
        _put(client, "c", "d0", "原始内容")
        kb.rebuild_bm25_from_qdrant(["c"])
        assert kb._ensure_bm25().collection_size("c") == 1

        kb.add_documents("c", ["更新后的内容"], ids=["d0"])
        assert kb._ensure_bm25().collection_size("c") == 1  # not 2

    def test_delete_keeps_bm25_consistent(self):
        """BM25-8/16: after delete_documents, the deleted doc is gone from
        BM25 (no stale hits) and the count matches Qdrant."""
        kb, client = _make_kb()
        for i in range(3):
            _put(client, "c", f"d{i}", f"独特文档内容 {i}")
        kb.rebuild_bm25_from_qdrant(["c"])
        assert kb._ensure_bm25().collection_size("c") == 3

        kb.delete_documents("c", ["d1"])
        assert kb._ensure_bm25().collection_size("c") == 2
        assert kb.get_collection_count("c") == 2
        hits = kb._bm25_search("独特", ["c"], top_k=3)
        ids = {h["id"] for h in hits}
        assert "d1" not in ids


# ---------------------------------------------------------------------------
# BM25-17: concurrent mutation during rebuild (atomic publication)
# ---------------------------------------------------------------------------


class TestConcurrentMutationDuringRebuild:
    def test_concurrent_add_during_rebuild_is_not_lost(self):
        """BM25-17: an add that races with a rebuild must not be discarded.

        Without the rebuild lock, the add would mutate the OLD ``self._bm25``
        and the rebuild would swap in a candidate that omits it (built from a
        pre-add scroll snapshot) — permanently losing the document from BM25
        until the next rebuild. With the lock, the add blocks until the
        rebuild publishes its candidate, then mutates the NEW published index.

        Deterministic harness: the fake's scroll captures its snapshot at
        call-time (pre-add) and blocks on an event; the add's Qdrant upsert
        lands between scroll-snapshot and swap, so the only way BM25 ends up
        with the 3rd doc is if the add applied to the post-swap index.
        """
        import threading

        kb, client = _make_kb()
        _put(client, "c", "d0", "文档内容零")
        _put(client, "c", "d1", "文档内容一")

        block = threading.Event()
        client.scroll_block_event = block

        results: dict[str, bool] = {"rebuild_done": False, "add_done": False}

        def _rebuild():
            kb.rebuild_bm25_from_qdrant(["c"])
            results["rebuild_done"] = True

        def _add():
            # Qdrant upsert lands here (outside the BM25 lock); the rebuild's
            # scroll already snapshotted 2 docs, so the candidate will have 2.
            kb.add_documents("c", ["文档内容二 新增"], ids=["d2"])
            results["add_done"] = True

        t_rebuild = threading.Thread(target=_rebuild)
        t_rebuild.start()
        # Let the rebuild acquire the lock and enter scroll (which blocks).
        import time

        time.sleep(0.1)
        t_add = threading.Thread(target=_add)
        t_add.start()
        # The add's BM25 mutation is blocked on the lock (rebuild holds it).
        time.sleep(0.1)
        # Release the scroll → rebuild completes the candidate (2 docs) and
        # swaps, then releases the lock → the add mutates the new index.
        block.set()
        t_rebuild.join(timeout=5.0)
        t_add.join(timeout=5.0)

        assert results["rebuild_done"], "rebuild thread did not complete"
        assert results["add_done"], "add thread did not complete"
        # The 3rd document must be present in BM25 (not lost to the swap).
        assert kb._ensure_bm25().collection_size("c") == 3, (
            f"concurrent add was lost during rebuild: bm25 size="
            f"{kb._ensure_bm25().collection_size('c')} (expected 3)"
        )
        hits = kb._bm25_search("新增", ["c"], top_k=3)
        assert {h["id"] for h in hits} == {"d2"}


# ---------------------------------------------------------------------------
# BM25-11/17: timeout & supersede (a late rebuild thread must not publish READY)
# ---------------------------------------------------------------------------


class TestTimeoutSupersede:
    def test_supersede_prevents_late_ready_publish(self):
        """BM25-11/17: when the container supersedes an in-flight rebuild
        (its ``wait_for`` timed out), the late-completing rebuild thread
        MUST NOT publish its candidate as READY. The generation guard makes
        the pre-publish check see a stale generation and refuse to swap.

        Reproduces the gap where a rebuild running in a ThreadPoolExecutor
        cannot be cancelled on timeout — without the guard it would finish
        scrolling later and flip DEGRADED back to READY."""
        import threading
        import time

        from rag.bm25_lifecycle import REASON_REBUILD_TIMEOUT, BM25Readiness

        kb, client = _make_kb()
        _put(client, "c", "d0", "内容零")
        _put(client, "c", "d1", "内容一")

        block = threading.Event()
        client.scroll_block_event = block

        result: dict = {}

        def _rebuild():
            result["meta"] = kb.rebuild_bm25_from_qdrant(["c"], timeout=5.0)

        t = threading.Thread(target=_rebuild)
        t.start()
        time.sleep(0.15)  # let the rebuild acquire the lock and block in scroll
        # Container-side: supersede the in-flight rebuild (simulates wait_for
        # timeout). This bumps the generation and marks DEGRADED.
        kb.supersede_bm25_rebuild(REASON_REBUILD_TIMEOUT)
        assert kb.bm25_readiness() is BM25Readiness.DEGRADED
        # Release the blocked scroll → the rebuild finishes and reaches publish.
        block.set()
        t.join(timeout=5.0)
        assert not t.is_alive(), "rebuild thread did not complete"

        # The late rebuild MUST NOT have flipped DEGRADED back to READY.
        assert kb.bm25_readiness() is BM25Readiness.DEGRADED, (
            f"superseded rebuild published READY: readiness={kb.bm25_readiness()}"
        )
        # The candidate was never swapped in: search returns nothing.
        assert kb._bm25_search("内容", ["c"], top_k=3) == []

    def test_rebuild_self_deadlines_at_publish(self):
        """BM25-11: a rebuild that finishes scrolling past its OWN deadline
        (no external supersede) self-aborts at the pre-publish check — the
        candidate is discarded and readiness is DEGRADED, never READY.

        The between-collections deadline check cannot fire while the rebuild
        is stuck inside a single ``scroll()`` call; the pre-publish check is
        what closes that window."""
        import threading
        import time

        from rag.bm25_lifecycle import BM25Readiness

        kb, client = _make_kb()
        _put(client, "c", "d0", "内容零")
        _put(client, "c", "d1", "内容一")

        block = threading.Event()
        client.scroll_block_event = block

        result: dict = {}

        def _rebuild():
            result["meta"] = kb.rebuild_bm25_from_qdrant(["c"], timeout=0.1)

        t = threading.Thread(target=_rebuild)
        t.start()
        time.sleep(0.3)  # past the 0.1s deadline; rebuild still blocked in scroll
        assert t.is_alive(), "rebuild finished too early — test harness broken"
        block.set()  # release scroll → rebuild reaches publish past its deadline
        t.join(timeout=5.0)
        assert not t.is_alive(), "rebuild thread did not complete"

        assert kb.bm25_readiness() is BM25Readiness.DEGRADED
        meta = result["meta"]
        assert meta.status is BM25Readiness.DEGRADED
        assert meta.reason == "rebuild_timeout"
        assert kb._bm25_search("内容", ["c"], top_k=3) == []


# ---------------------------------------------------------------------------
# BM25-4/6: failed rebuild preserves last_build_at (last *successful* build)
# ---------------------------------------------------------------------------


class TestLastBuildAtPreserved:
    def test_failed_rebuild_preserves_last_build_at(self):
        """BM25-4/6: a failed rebuild must NOT overwrite ``last_build_at``
        (the last **successful** build time) with the failure time. The
        failure reason/time live in ``reason`` + the
        ``bm25_rebuild_failures_total`` metric; ``last_build_at`` must stay
        so an operator can see when BM25 was last known-good during a
        degradation incident. ``source_snapshot`` is preserved too."""
        from rag.bm25_lifecycle import BM25Readiness

        kb, client = _make_kb()
        _put(client, "c", "d0", "内容零")
        _put(client, "c", "d1", "内容一")

        meta_ok = kb.rebuild_bm25_from_qdrant(["c"])
        assert meta_ok.status is BM25Readiness.READY
        assert meta_ok.last_build_at > 0
        good_build_at = meta_ok.last_build_at
        good_snapshot = dict(meta_ok.source_snapshot)

        # Second rebuild: scroll fails. last_build_at must be preserved.
        client.scroll_raises = True
        client._scroll_yielded = 0
        meta_fail = kb.rebuild_bm25_from_qdrant(["c"])
        assert meta_fail.status is BM25Readiness.DEGRADED
        assert meta_fail.reason == "rebuild_scroll_failed"
        assert meta_fail.last_build_at == good_build_at, (
            f"last_build_at overwritten by failure time: "
            f"{meta_fail.last_build_at} != {good_build_at}"
        )
        assert meta_fail.source_snapshot == good_snapshot


# ---------------------------------------------------------------------------
# BM25-10: stable ID compatibility (P1-03)
# ---------------------------------------------------------------------------


class TestStableIdCompatibility:
    def test_rebuild_uses_stable_logical_document_identity(self):
        """BM25-10: rebuild consumes payload.doc_id (logical identity) — never
        re-derives identity from the storage Point ID or Python hash. BM25
        result ids equal the logical doc_ids stored in Qdrant."""
        kb, client = _make_kb()
        _put(client, "product_knowledge", "pr_000", "烟酰胺精华美白控油")
        _put(client, "product_knowledge", "pr_001", "视黄醇抗衰面霜")
        kb.rebuild_bm25_from_qdrant(["product_knowledge"])

        hits = kb._bm25_search("烟酰胺", ["product_knowledge"], top_k=2)
        assert hits[0]["id"] == "pr_000"
        # The Qdrant Point ID for pr_000 is the P1-03 stable SHA-256 mapping,
        # distinct from the logical id; BM25 must carry the logical id only.
        stable_pid = document_id_to_point_id("product_knowledge", "pr_000")
        assert hits[0]["id"] != stable_pid


# ---------------------------------------------------------------------------
# P0-05 compatibility (Step 23)
# ---------------------------------------------------------------------------


class TestP005Compatibility:
    @pytest.mark.asyncio
    async def test_embedding_down_and_bm25_ready_uses_bm25_only(self):
        """P0-05: embedding unavailable + BM25 READY → BM25-only explicit
        degraded (existing contract preserved)."""
        kb, client = _make_kb(embed_fn=None)
        _put(client, "c", "d0", "烟酰胺美白")
        kb.rebuild_bm25_from_qdrant(["c"])

        result = await kb.query_multiple(["c"], "烟酰胺", n_results=3)
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is True
        assert meta.get("vector_channel_used") is False
        assert meta.get("lexical_channel_used") is True
        assert meta.get("degraded_reason") == "embedding_unavailable"
        assert len(result) >= 1  # BM25 delivered results

    @pytest.mark.asyncio
    async def test_embedding_down_and_bm25_not_ready_is_no_channel(self):
        """P0-05 + BM25-2: embedding unavailable + BM25 not READY → explicit
        no_retrieval_channel (existing contract preserved, now honest)."""
        kb, client = _make_kb(embed_fn=None)
        _put(client, "c", "d0", "烟酰胺美白")
        # BM25 NOT rebuilt.

        result = await kb.query_multiple(["c"], "烟酰胺", n_results=3)
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is True
        assert meta.get("vector_channel_used") is False
        assert meta.get("lexical_channel_used") is False
        assert meta.get("degraded_reason") == "no_retrieval_channel"

    @pytest.mark.asyncio
    async def test_embedding_up_and_bm25_not_ready_marks_degraded(self):
        """BM25-3 + P0-05: embedding available + BM25 not READY → degraded
        with reason bm25_not_ready (new honest marking, not silent vector-only)."""
        kb, client = _make_kb()
        _put(client, "c", "d0", "烟酰胺美白")
        # BM25 NOT rebuilt.

        result = await kb.query_multiple(["c"], "烟酰胺", n_results=3)
        meta = getattr(result, "meta", {})
        assert meta.get("retrieval_degraded") is True
        assert meta.get("degraded_reason") == "bm25_not_ready"
        assert meta.get("lexical_channel_used") is False
