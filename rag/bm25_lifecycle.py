"""P1-02: BM25 lifecycle contract — readiness state + index metadata.

``BM25Retriever`` is a process-memory inverted index; ``Qdrant`` is the
persistent index. Without an explicit recovery step, a persistent-mode
restart leaves BM25 empty while Qdrant still has its points, and Hybrid
retrieval silently degrades to vector-only. This module pins the lifecycle
states and the index metadata that make that degradation **observable** and
**honest** instead of silent.

Readiness states (BM25-2):

    UNINITIALIZED — no rebuild has run yet (fresh process, or lazy init only)
    BUILDING      — a rebuild is in progress; the index is NOT usable yet
    READY         — a rebuild succeeded and the index was published; the
                    document count has been reconciled with the Qdrant source
    DEGRADED      — a rebuild failed (scroll error / timeout / Qdrant down);
                    BM25 is NOT usable; callers must not claim normal Hybrid

An empty ``BM25Retriever`` instance is NOT READY — ``self._bm25 is not None``
is not a readiness signal. Only a successful rebuild publishes READY via an
atomic reference swap (BM25-17/18: a partial index is never published).

Index metadata (BM25-6):

    index_version         — bumped on every successful rebuild AND on every
                             incremental add/delete (corpus-state change)
    document_count        — total docs across collections in the BM25 index
    last_build_at         — epoch seconds of the last successful full rebuild
    source_snapshot       — per-collection descriptor of the Qdrant data the
                             index was built from (point_count, doc_count)
    skipped_invalid       — points skipped during rebuild (missing doc_id /
                             content) — explains any source↔index gap
    mutations_since_build — incremental add/delete count since last full rebuild

This module contains ONLY types/helpers — the behavioural rebuild lives in
``rag.qdrant_knowledge_base``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BM25Readiness(str, Enum):
    """BM25 readiness state (str enum — stable values for logs/metrics).

    An empty ``BM25Retriever`` instance is NOT READY — ``self._bm25 is not
    None`` is not a readiness signal. Only a successful rebuild publishes
    READY via an atomic reference swap (BM25-17/18: a partial index is never
    published).
    """

    UNINITIALIZED = "uninitialized"  # no rebuild has run yet
    BUILDING = "building"  # rebuild in progress; index NOT usable
    READY = "ready"  # rebuild succeeded and was published
    DEGRADED = "degraded"  # rebuild failed; BM25 NOT usable


@dataclass
class BM25IndexMeta:
    """Traceable metadata for the current BM25 index (BM25-6).

    ``source_snapshot`` answers "which batch of Qdrant data was BM25 built
    from?" — it is NOT a full dataset provenance system (that is P2 scope).
    """

    index_version: int = 0
    document_count: int = 0
    last_build_at: float = 0.0
    source_snapshot: dict[str, Any] = field(default_factory=dict)
    status: BM25Readiness = BM25Readiness.UNINITIALIZED
    reason: str = ""
    skipped_invalid: int = 0
    mutations_since_build: int = 0


# Clock injection point — production uses wall-clock epoch seconds. Tests may
# override ``bm25_lifecycle._clock`` for deterministic ``last_build_at`` values.
_clock = time.time


def _now() -> float:
    """Current build timestamp (overridable for tests)."""
    return _clock()


# Reason codes (shared constants so log/metric labels stay consistent).
REASON_REBUILD_SCROLL_FAILED = "rebuild_scroll_failed"
REASON_REBUILD_TIMEOUT = "rebuild_timeout"
REASON_REBUILD_EXCEPTION = "rebuild_exception"
REASON_QDRANT_UNAVAILABLE = "qdrant_unavailable"
REASON_BM25_NOT_READY = "bm25_not_ready"
REASON_BM25_INIT_FAILED = "bm25_init_failed"
