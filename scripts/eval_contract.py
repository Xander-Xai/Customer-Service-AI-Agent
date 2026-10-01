#!/usr/bin/env python3
"""Canonical RAG evaluation contract (single source of truth).

Shared by ``scripts/evaluate_rag.py`` (producer),
``scripts/project_facts.py`` (facts + doc guards) and
``scripts/rag_evidence_status.py`` (formal-artifact validation). Keeping the
experiment names, metric families, K values, population views and failure
taxonomy here removes the previous duplicate definitions / fragile regex
parsing of the evaluator source.

This module is pure data + tiny pure helpers: it imports nothing from the
application, so it is safe to import from audits, CI guards and tests.
"""

from __future__ import annotations

from typing import Any

REPORT_SCHEMA_VERSION = "rag-eval-evidence/v2"

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
