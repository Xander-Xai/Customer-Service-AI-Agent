"""P1-01: RetrievalTrace validation (spec step 27).

Produces an actual ``RetrievalTrace`` transcript for four scenarios:

  A. healthy hybrid request     (embedding available + BM25 READY)
  B. scene-filtered request     (scene filter applied)
  C. embedding degraded request (embedding down + BM25 ready → lexical degraded)
  D. BM25 degraded request       (embedding up + BM25 not ready → vector degraded)

Run: ``PYTHONPATH=. python scripts/repro_p1_01_retrieval_trace.py``
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.bm25_retriever import BM25Retriever
from rag.qdrant_knowledge_base import _EMBEDDING_DIM, QdrantKnowledgeBase
from rag.retrieval_contract import RetrievalRequest


def _make_kb(embed_fn=None):
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
        mock_client.count.return_value = MagicMock(count=0)
        mock_client.query_points.return_value = MagicMock(points=[])
        kb = QdrantKnowledgeBase(host="localhost", port=6333)
        kb._hybrid_enabled = True
        return kb, mock_client


def _seed_bm25(kb, collection, docs):
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
            "mode": "trace_repro",
        },
        status=BM25Readiness.READY,
        reason="",
    )


def _point(doc_id, content, score=0.9, scene=None):
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


def _print_trace(label, result):
    print(f"\n=== {label} ===")
    meta = result.meta
    print(
        f"meta: degraded={meta.get('retrieval_degraded')} "
        f"reason={meta.get('degraded_reason')!r} "
        f"vector={meta.get('vector_channel_used')} "
        f"lexical={meta.get('lexical_channel_used')}"
    )
    print(f"evidence_count={len(result)} " f"rewritten_query={result.trace.rewritten_query[:60]!r}")
    print("stages:")
    for s in result.trace.stages:
        print(
            f"  {s.name:12s} {s.status.value:9s} "
            f"in={s.candidate_in} out={s.candidate_out} "
            f"{s.duration_ms:.2f}ms reason={s.reason!r}"
        )


async def main():
    # A. healthy hybrid
    kbA, mcA = _make_kb(embed_fn=_embed_fn())
    _seed_bm25(kbA, "product_knowledge", ["烟酰胺精华液功效说明"])
    mcA.query_points.return_value = MagicMock(points=[_point("d1", "烟酰胺精华液功效")])
    resA = await kbA.retrieve(RetrievalRequest(query="烟酰胺", collections=["product_knowledge"]))
    _print_trace("A. healthy hybrid", resA)

    # B. scene-filtered
    kbB, mcB = _make_kb(embed_fn=_embed_fn())
    _seed_bm25(kbB, "complaint_knowledge", ["过敏投诉处理流程"])
    mcB.query_points.return_value = MagicMock(
        points=[_point("d1", "过敏投诉处理", scene="投诉处理")]
    )
    resB = await kbB.retrieve(
        RetrievalRequest(query="面霜过敏", collections=["complaint_knowledge"], scene="投诉处理")
    )
    _print_trace("B. scene-filtered", resB)

    # C. embedding degraded (down + BM25 ready)
    kbC, mcC = _make_kb(embed_fn=None)
    _seed_bm25(kbC, "product_knowledge", ["玻尿酸保湿锁水"])
    resC = await kbC.retrieve(RetrievalRequest(query="玻尿酸", collections=["product_knowledge"]))
    _print_trace("C. embedding degraded", resC)

    # D. BM25 degraded (up + BM25 not ready)
    kbD, mcD = _make_kb(embed_fn=_embed_fn())
    mcD.query_points.return_value = MagicMock(points=[_point("d1", "水杨酸去角质")])
    resD = await kbD.retrieve(RetrievalRequest(query="水杨酸", collections=["product_knowledge"]))
    _print_trace("D. BM25 degraded", resD)

    # Sanity assertions
    assert not resA.meta["retrieval_degraded"], "A should be non-degraded"
    assert (
        resC.meta["retrieval_degraded"] and resC.meta["lexical_channel_used"]
    ), "C should be lexical-degraded"
    assert (
        resD.meta["retrieval_degraded"] and resD.meta["vector_channel_used"]
    ), "D should be vector-degraded"
    print("\nP1-01 TRACE VALIDATION: PASS")
    print(json.dumps({"A_trace": resA.trace.to_dict(), "D_trace": resD.trace.to_dict()}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
