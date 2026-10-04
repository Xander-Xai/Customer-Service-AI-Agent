#!/usr/bin/env python3
"""Canonical RAG evaluation contract (single source of truth).

Shared by ``scripts/evaluate_rag.py`` (producer),
``scripts/project_facts.py`` (facts + doc guards) and
``scripts/rag_evidence_status.py`` (formal-artifact validation). Keeping the
experiment names, metric families, K values, population views, failure
taxonomy and evidence-validity thresholds here removes the previous duplicate
definitions / fragile regex parsing of the evaluator source.

This module is pure data + tiny pure helpers: it imports nothing from the
application, so it is safe to import from audits, CI guards and tests.
"""

from __future__ import annotations

from typing import Any

# v3 adds the mandatory ``evidence_validity`` block (issue #45): a run status
# of VERIFIED_FULL is only self-certifying when the evidence-validity predicate
# (scripts/rag_evidence_validity.py) holds, so the artifact schema carries the
# machine-checkable measurements the predicate is recomputed from.
REPORT_SCHEMA_VERSION = "rag-eval-evidence/v3"

EXPERIMENT_NAMES: tuple[str, ...] = (
    "vector_only",
    "bm25_only",
    "hybrid_no_rerank",
    "hybrid_rerank",
)

# vector_only: 仅向量通道（禁用 BM25），无 rerank
# bm25_only: 仅词法通道（embedding 置空 -> 向量通道 fail-closed 禁用），无 rerank
# hybrid_no_rerank: 生产混合检索（vector+BM25+RRF），无 rerank
# hybrid_rerank: 生产混合检索 + ApiReranker（production-like）
EXPERIMENT_SPECS: dict[str, dict[str, Any]] = {
    "vector_only": {"disable_hybrid": True, "disable_embedding": False, "rerank": False},
    "bm25_only": {"disable_hybrid": False, "disable_embedding": True, "rerank": False},
    "hybrid_no_rerank": {"disable_hybrid": False, "disable_embedding": False, "rerank": False},
    "hybrid_rerank": {"disable_hybrid": False, "disable_embedding": False, "rerank": True},
}

DEFAULT_KS: tuple[int, ...] = (1, 3, 5, 8)

# ---------------------------------------------------------------------------
# Evidence validity contract (issue #45)
# ---------------------------------------------------------------------------
#
# ``VERIFIED`` means *the evidence itself is valid*, not "the process finished".
# A 649-query run that completes 649/649 requests through a dead retrieval
# channel, over a corpus where the gold documents were never indexed, produces
# a structurally perfect artifact whose metrics are meaningless. These
# thresholds are therefore part of the *contract*, not tuning knobs: a run that
# violates any of them may still emit a full diagnostic artifact, but it may
# not self-certify as formal evidence.

VALIDITY_CONTRACT_VERSION = "rag-evidence-validity/v1"

VERDICT_VALID = "VALID"
VERDICT_INVALID = "INVALID"

#: Channel identities a single ablation leg can require. Derived from
#: EXPERIMENT_SPECS — a leg never declares its own channel list.
CHANNEL_VECTOR = "vector"
CHANNEL_BM25 = "bm25"
CHANNEL_RERANK = "rerank"
CHANNELS: tuple[str, ...] = (CHANNEL_VECTOR, CHANNEL_BM25, CHANNEL_RERANK)

#: Maximum share of a leg's queries allowed to have run on a degraded
#: (fallback) retrieval path. A formal metric averaged over two different
#: retrieval regimes is not a reproducible single-regime measurement. The
#: 100%-degraded case is reported separately (``FULLY_DEGRADED``) because it
#: means the channel was dead for the entire population.
MAX_ACCEPTABLE_DEGRADED_RATIO = 0.10

#: Maximum share of queries whose gold documents are absent from the indexed
#: corpus. Above this, the run measures the corpus import, not the retriever.
MAX_GOLD_NOT_INDEXED_RATIO = 0.10

#: Corpus coverage floors, expressed against the end-to-end ``all_queries``
#: population (the primary view's denominator).
MIN_RETRIEVAL_ELIGIBLE_RATIO = 0.90
MIN_FULL_GOLD_COVERED_RATIO = 0.80

#: Preflight must be clean (``OK``) for the 4-config ablation to be formal.
FORMAL_PREFLIGHT_STATUS = "OK"


def required_channels(experiment: str) -> tuple[str, ...]:
    """Retrieval channels a given ablation leg must actually exercise.

    Derived from ``EXPERIMENT_SPECS`` so the requirement can never drift from
    the override that defines the leg:

    - ``disable_embedding`` -> vector channel is forced off (bm25_only)
    - ``disable_hybrid``    -> lexical/BM25 channel is forced off (vector_only)
    - ``rerank``            -> reranker must apply (hybrid_rerank)
    """
    spec = EXPERIMENT_SPECS[experiment]
    channels: list[str] = []
    if not spec["disable_embedding"]:
        channels.append(CHANNEL_VECTOR)
    if not spec["disable_hybrid"]:
        channels.append(CHANNEL_BM25)
    if spec["rerank"]:
        channels.append(CHANNEL_RERANK)
    return tuple(channels)


# Documented metric family (``{k}`` is substituted per K). ``mrr`` is rank-
# truncated at top-K and is documented as ``MRR@K``. ``first_relevant_rank`` is
# an internal accounting field, not a reported quality metric.
METRIC_FAMILIES: tuple[str, ...] = (
    "hit@{k}",
    "recall@{k}",
    "precision@{k}",
    "ndcg@{k}",
    "mrr@{k}",
)

POPULATION_VIEWS: tuple[str, ...] = (
    "all_queries",
    "retrieval_eligible",
    "full_gold_covered",
)

POPULATION_DEFINITIONS: dict[str, str] = {
    "all_queries": (
        "View A — end-to-end：全部查询进入分母；gold 未进入索引或检索失败"
        "（GOLD_NOT_INDEXED / TIMEOUT / PROVIDER_ERROR）仍按 0 分计入。"
        "衡量 corpus coverage + indexing + retrieval algorithm 的系统级结果"
        "（主口径）。"
    ),
    "retrieval_eligible": (
        "View B — retrieval eligible：至少 1 个 gold document 已进入 "
        "corpus/index 的查询（数量运行时动态计算）。用于分析 retriever 在"
        "「至少存在可命中文档」情况下的表现。"
    ),
    "full_gold_covered": (
        "View C — full-gold-covered：全部 gold documents 都存在于当前 "
        "corpus/index 的查询（数量运行时动态计算）。适合 Recall@K / NDCG，"
        "避免 gold 缺失直接压低算法指标。"
    ),
}

FAILURE_TAXONOMY: tuple[str, ...] = (
    "TIMEOUT",
    "PROVIDER_ERROR",
    "GOLD_NOT_INDEXED",
    "MISS_ALL",
    "LOW_RANK",
)
