"""P1-01: Unified retrieval contract — request, result, trace.

Single canonical retrieval entrypoint and result contract. Replaces the
fragmented ``query`` / ``search`` / ``query_multiple`` semantics with one
owned pipeline:

    RetrievalRequest
          │
          ▼
    KnowledgeBase.retrieve
          │
          ▰ Rewrite → Scene/Metadata Filter → Vector + BM25 → RRF → Reranker
          ▰ Evidence (+ RetrievalTrace)

Design choices (spec P1-01):
  - ``RetrievalResult`` EXTENDS ``RetrievalResultList`` (IS-A list + ``.meta``)
    so the P0-05 / P1-02 degraded-metadata contract
    (``retrieval_degraded`` / ``vector_channel_used`` / ``lexical_channel_used``
    / ``degraded_reason``) is preserved byte-for-byte. A new ``.trace`` is the
    only addition — callers that iterate results or read ``.meta`` are
    unaffected.
  - ``RetrievalRequest`` carries only what the current project needs (spec:
    "不要为了未来过度设计"): query, collections (REQUIRED — no product_knowledge
    default), scene, metadata filters, top_k, rewrite/rerank policy, the LLM
    client (owned by the pipeline, not the agent), the AgentState (to surface
    degraded state + reuse prefetch), and optional prefetched reusable
    computation (rewritten query / embedding) to avoid duplicate work.
  - The request does NOT carry a new permission/version model — it only
    consumes the existing user_id scope (P0-04) and CachePolicy (P0-02)
    elsewhere. Retrieval filters by collection + scene + metadata, never by
    user identity (that remains the cache's job).

This module contains ONLY types — the behavioural orchestration lives in
``rag.qdrant_knowledge_base.QdrantKnowledgeBase.retrieve``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rag.embedding_status import RetrievalResultList

# ---------------------------------------------------------------------------
# Stage identity (spec step 6: REWRITE/FILTER/VECTOR/BM25/FUSION_RRF/RERANK/FINAL)
# ---------------------------------------------------------------------------

STAGE_REWRITE = "REWRITE"
STAGE_FILTER = "FILTER"
STAGE_VECTOR = "VECTOR"
STAGE_BM25 = "BM25"
STAGE_FUSION_RRF = "FUSION_RRF"
STAGE_RERANK = "RERANK"
STAGE_FINAL = "FINAL"

#: The canonical stage ordering (spec step 9). retrieve() records stages in
#: this order; the trace is a faithful transcript of one request's pipeline.
STAGE_ORDER: tuple[str, ...] = (
    STAGE_REWRITE,
    STAGE_FILTER,
    STAGE_VECTOR,
    STAGE_BM25,
    STAGE_FUSION_RRF,
    STAGE_RERANK,
    STAGE_FINAL,
)


class StageStatus(str, Enum):
    """Per-stage execution state (spec step 6).

    EXECUTED  — the stage ran and produced output
    SKIPPED   — the stage was deliberately not run (policy / not-needed)
    DEGRADED  — the stage ran but a channel was unavailable / timed out; the
                result is real but must be labelled degraded, never normal
    """

    EXECUTED = "executed"
    SKIPPED = "skipped"
    DEGRADED = "degraded"


@dataclass
class TraceStage:
    """One pipeline stage's record (spec step 6).

    Records status, candidate counts (in/out), latency, and a degraded
    reason — never sensitive query/content text.
    """

    name: str
    status: StageStatus
    candidate_in: int = 0
    candidate_out: int = 0
    duration_ms: float = 0.0
    reason: str = ""


@dataclass
class RetrievalTrace:
    """A transcript of one retrieval request's pipeline (spec step 6/27).

    ``rewritten_query`` is the single canonical rewritten query (spec step 10)
    — visible so one request cannot silently rewrite twice.
    """

    stages: list[TraceStage] = field(default_factory=list)
    rewritten_query: str = ""

    def add(self, stage: TraceStage) -> TraceStage:
        self.stages.append(stage)
        return stage

    def stage(self, name: str) -> TraceStage | None:
        """The record for ``name``, or None if absent."""
        for s in self.stages:
            if s.name == name:
                return s
        return None

    def stage_status(self, name: str) -> StageStatus | None:
        s = self.stage(name)
        return s.status if s is not None else None

    def to_dict(self) -> dict[str, Any]:
        """Serializable form (no query/content text)."""
        return {
            "rewritten_query_present": bool(self.rewritten_query),
            "stages": [
                {
                    "name": s.name,
                    "status": s.status.value,
                    "candidate_in": s.candidate_in,
                    "candidate_out": s.candidate_out,
                    "duration_ms": round(s.duration_ms, 3),
                    "reason": s.reason,
                }
                for s in self.stages
            ],
        }


# ---------------------------------------------------------------------------
# Result (extends RetrievalResultList so P0-05/P1-02 .meta is preserved)
# ---------------------------------------------------------------------------


class RetrievalResult(RetrievalResultList):
    """Unified retrieval result: evidence list + degraded ``.meta`` (P0-05 /
    P1-02) + ``.trace`` (P1-01).

    IS-A ``RetrievalResultList`` (itself IS-A ``list``): ``len()``, iteration,
    indexing, ``isinstance(list)`` and the ``.meta`` contract all behave as
    before. The only addition is ``.trace``; callers that do not read it are
    unaffected. ``.trace`` always exists (empty trace if none provided).
    """

    def __init__(
        self,
        seq: Any = (),
        *,
        meta: dict[str, Any] | None = None,
        trace: RetrievalTrace | None = None,
    ) -> None:
        super().__init__(seq, meta=meta)
        self.trace: RetrievalTrace = trace if trace is not None else RetrievalTrace()


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalRequest:
    """The single canonical retrieval input (spec step 5).

    ``collections`` is REQUIRED — the old ``search()`` default of
    ``product_knowledge`` was a scene-bypass bug; the unified contract forces
    the caller to name its target collections.

    ``llm`` is the LLM client for query rewrite, OWNED BY THE PIPELINE (spec
    step 10): the agent no longer rewrites the query itself and then calls a
    KB that rewrites again. retrieve() performs one canonical rewrite
    (optional LLM rewrite + synonym expansion) and records it in the trace.

    ``prefetched_rewritten_query`` / ``prefetched_embedding`` are reusable
    computation from the graph prefetch (spec step 18/20): when supplied,
    retrieve() reuses them instead of recomputing — but it STILL applies the
    request's scene/filter and runs the full pipeline. Prefetch is never final
    evidence.
    """

    query: str
    collections: list[str]
    scene: str | None = None
    metadata_filters: dict[str, Any] | None = None
    top_k: int = 3
    rewrite: bool = True
    rerank: bool = True
    llm: Any = None
    state: dict[str, Any] | None = None
    prefetched_rewritten_query: str | None = None
    prefetched_embedding: list[float] | None = None
    prefetched_embedding_status: Any = None
    retrieval_timeout: float | None = None
