"""P1-02 Step 2 — Restart reproduction (BEFORE any fix).

Proves the silent BM25 degradation on persistent Qdrant restart using a REAL
local-path Qdrant (qdrant-client 1.18.0 embedded mode, no server).

Instance A: add documents -> Qdrant populated + BM25 populated -> close.
Instance B: SAME persistent path, NO seed/add_documents (persistent mode) ->
            Qdrant still has data, but BM25 is empty -> hybrid silently
            degrades to vector-only while still claiming non-degraded.

This script does NOT modify production code. It injects a path-mode client
and a deterministic embed_fn so the lifecycle — not the embedding provider —
is what's under test.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from qdrant_client import QdrantClient
from qdrant_client.http import models

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.qdrant_knowledge_base import _EMBEDDING_DIM, QdrantKnowledgeBase

DOCS = [
    ("pr_000", "烟酰胺精华具有美白和控油功效，适合油性肌肤"),
    ("pr_001", "视黄醇抗衰老面霜能促进胶原蛋白生成，夜间使用"),
    ("pr_002", "水杨酸洁面乳深层清洁毛孔，适合痘痘肌肤"),
]
COLLECTION = "product_knowledge"


def _make_kb(path: str) -> QdrantKnowledgeBase:
    """Build a QdrantKnowledgeBase backed by a local-path Qdrant client.

    Production __init__ only takes host/port; for the repro we inject a real
    path-mode client so persistence is genuine (not mocked). A deterministic
    embed_fn stands in for the real provider — the repro tests BM25/Qdrant
    lifecycle, not embedding quality.
    """
    # Construct via the normal path but with a bad host so the internal
    # QdrantClient(host=...) connection fails cleanly; then replace the client
    # with a real path-mode one and mark available.
    kb = QdrantKnowledgeBase(host="127.0.0.1", port=1, embedding_model=MagicMock())
    client = QdrantClient(path=path)
    kb._client = client
    kb._available = True
    kb._collection_cache.clear()

    # Deterministic embed_fn so add_documents persists real vectors.
    def _embed(texts):
        import hashlib

        out = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            vec = [(b / 255.0 - 0.5) for b in h[:_EMBEDDING_DIM]]
            # pad/truncate to exact dim deterministically
            vec = (vec * ((_EMBEDDING_DIM // len(vec)) + 1))[:_EMBEDDING_DIM]
            out.append(vec)
        return out

    kb._embed_fn = MagicMock()
    kb._embed_fn.encode = _embed
    kb._embed_fn.model = "repro-deterministic"
    kb._embedding_model_name = "repro-deterministic"
    kb._embedding_provider = "ReproEmbed"
    return kb


def _force_collection(kb: QdrantKnowledgeBase, name: str) -> None:
    kb._client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(size=_EMBEDDING_DIM, distance=models.Distance.COSINE),
    )
    kb._collection_cache[name] = True


def main() -> int:
    """Demonstrate the P1-02 restart lifecycle (before/after the fix).

    Three observations:
      [A] Instance A populates Qdrant + BM25 in-process.
      [B] Instance B restarts on the SAME persistent path with NO seed/add.
          Before the fix: BM25 empty + hybrid claims non-degraded (silent
          vector-only). After the fix: BM25 still empty (no rebuild called)
          BUT the hybrid honestly marks ``retrieval_degraded=True,
          reason='bm25_not_ready'`` — the degradation is observable.
      [C] Instance B + ``rebuild_bm25_from_qdrant``: BM25 recovers from the
          persisted Qdrant points and hybrid returns to normal.
    """
    path = tempfile.mkdtemp(prefix="p1_02_repro_")
    print(f"[repro] persistent Qdrant path: {path}")
    failures: list[str] = []

    try:
        # ---- [A] Instance A: populate ----
        kbA = _make_kb(path)
        _force_collection(kbA, COLLECTION)
        ids = [d[0] for d in DOCS]
        docs = [d[1] for d in DOCS]
        kbA.add_documents(COLLECTION, docs, ids=ids)
        qdrant_count_A = kbA.get_collection_count(COLLECTION)
        bm25_size_A = kbA._ensure_bm25().collection_size(COLLECTION)
        print(f"[A] qdrant_count={qdrant_count_A} bm25_size={bm25_size_A}")
        kbA._client.close()
        del kbA

        import asyncio

        # ---- [B] Instance B: restart, SAME path, NO seed/add (persistent mode) ----
        kbB = _make_kb(path)
        qdrant_count_B = kbB.get_collection_count(COLLECTION)
        bm25 = kbB._ensure_bm25()  # lazily creates an EMPTY BM25Retriever
        bm25_size_B = bm25.collection_size(COLLECTION)
        bm25_hits_B = kbB._bm25_search("烟酰胺 美白", [COLLECTION], top_k=3)
        result_b = asyncio.get_event_loop().run_until_complete(
            kbB.query_multiple([COLLECTION], "烟酰胺 美白", n_results=3)
        )
        meta_b = getattr(result_b, "meta", {})
        print(
            f"[B] qdrant_count={qdrant_count_B} bm25_size={bm25_size_B} "
            f"bm25_hits={len(bm25_hits_B)} hybrid_degraded={meta_b.get('retrieval_degraded')} "
            f"vector_used={meta_b.get('vector_channel_used')} "
            f"lexical_used={meta_b.get('lexical_channel_used')} "
            f"reason={meta_b.get('degraded_reason')!r}"
        )

        # ---- [C] Instance B + rebuild from persisted Qdrant ----
        meta_c = kbB.rebuild_bm25_from_qdrant([COLLECTION])
        bm25_size_C = kbB._ensure_bm25().collection_size(COLLECTION)
        bm25_hits_C = kbB._bm25_search("烟酰胺 美白", [COLLECTION], top_k=3)
        result_c = asyncio.get_event_loop().run_until_complete(
            kbB.query_multiple([COLLECTION], "烟酰胺 美白", n_results=3)
        )
        meta_cqm = getattr(result_c, "meta", {})
        print(
            f"[C] after rebuild: bm25_size={bm25_size_C} bm25_hits={len(bm25_hits_C)} "
            f"hybrid_degraded={meta_cqm.get('retrieval_degraded')} "
            f"lexical_used={meta_cqm.get('lexical_channel_used')} "
            f"version={meta_c.index_version} doc_count={meta_c.document_count}"
        )
        kbB._client.close()

        # ---- Assertions ----
        if qdrant_count_B != qdrant_count_A:
            failures.append(f"Qdrant did NOT persist: A={qdrant_count_A} B={qdrant_count_B}")
        # [B] no rebuild → BM25 empty + degradation MUST be observable (not silent)
        if bm25_size_B != 0:
            failures.append(f"[B] BM25 unexpectedly populated without rebuild: {bm25_size_B}")
        if (
            meta_b.get("retrieval_degraded") is not True
            or meta_b.get("degraded_reason") != "bm25_not_ready"
        ):
            failures.append(
                "[B] BM25-3: empty BM25 must be honestly degraded (bm25_not_ready), "
                f"got degraded={meta_b.get('retrieval_degraded')} reason={meta_b.get('degraded_reason')!r}"
            )
        # [C] rebuild → BM25 recovered + hybrid normal
        if bm25_size_C != qdrant_count_A:
            failures.append(
                f"[C] rebuild did not recover all docs: bm25={bm25_size_C} qdrant={qdrant_count_A}"
            )
        if not bm25_hits_C:
            failures.append("[C] BM25 search returned no hits after rebuild")
        if (
            meta_cqm.get("retrieval_degraded") is not False
            or meta_cqm.get("lexical_channel_used") is not True
        ):
            failures.append(
                "[C] after rebuild hybrid must be non-degraded with lexical channel used, "
                f"got degraded={meta_cqm.get('retrieval_degraded')} lexical={meta_cqm.get('lexical_channel_used')}"
            )
    finally:
        shutil.rmtree(path, ignore_errors=True)

    print("\n=== P1-02 RESTART LIFECYCLE RESULT ===")
    if failures:
        print("FAIL — lifecycle contract broken:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — restart degradation is observable (B) and rebuild recovers (C).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
