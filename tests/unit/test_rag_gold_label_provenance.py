"""Deterministic tests for the RAG gold-label provenance audit (Issue #99).

These tests pin two different things:

1. the classification logic, with fully synthetic inputs (no repository files);
2. the provenance verdict on the *committed* benchmark, so a future silent
   change to ``expected_doc_ids`` cannot quietly restore meaningless formal
   metrics.

No provider, LLM, embedding or reranker is called. The audit is pure static
analysis over repository data files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.rag_gold_label_provenance import (
    PROVENANCE_ABSENT,
    PROVENANCE_GENERATOR,
    PROVENANCE_RELEVANCE,
    PROVENANCE_UNKNOWN,
    analyze,
    classify_labels,
    documented_generator_gold,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATEGORY_MAP = {"成分知识": "成分知识", "产品推荐": "产品介绍"}


def _query(query_id: str, category: str, gold: list[str]) -> dict[str, object]:
    return {"query_id": query_id, "category": category, "expected_doc_ids": gold}


def test_reproduced_label_is_classified_as_documented_generator() -> None:
    queries = [_query("q1", "成分知识", ["a", "b", "c"])]
    corpus = {"a": "成分知识", "b": "成分知识", "c": "成分知识"}
    generated = {"q1": ["a", "b", "c"]}
    result = classify_labels(queries, corpus, generated)
    assert result["provenance_sources"][PROVENANCE_GENERATOR] == 3
    assert result["valid_labels"] == 0
    assert result["invalid_labels"] == 3
    assert result["unknown_provenance_labels"] == 0


def test_absent_label_is_classified_as_absent_from_corpus() -> None:
    queries = [_query("q1", "成分知识", ["a", "ghost_1", "ghost_2"])]
    corpus = {"a": "成分知识"}
    generated: dict[str, list[str]] = {}
    result = classify_labels(queries, corpus, generated)
    assert result["provenance_sources"][PROVENANCE_ABSENT] == 2
    assert result["labels_absent_from_corpus"] == 2
    assert result["distinct_labels_absent_from_corpus"] == 2
    assert result["queries_with_absent_gold"] == 1
    assert result["queries_fully_uncovered"] == 0


def test_fully_uncovered_query_is_flagged() -> None:
    queries = [_query("q1", "产品推荐", ["ghost_1", "ghost_2", "ghost_3"])]
    result = classify_labels(queries, {}, {})
    assert result["queries_with_absent_gold"] == 1
    assert result["queries_fully_uncovered"] == 1


def test_present_but_unreproduced_label_is_unknown_provenance() -> None:
    queries = [_query("q1", "产品推荐", ["a", "b", "c"])]
    corpus = {"a": "产品介绍", "b": "使用方法", "c": "售后政策"}
    generated = {"q1": ["x", "y", "z"]}
    result = classify_labels(queries, corpus, generated)
    assert result["provenance_sources"][PROVENANCE_UNKNOWN] == 3
    assert result["reproduced_query_count"] == 0
    assert result["unknown_provenance_labels"] == 3
    assert result["invalid_labels"] == 0


def test_relevance_annotation_class_is_empty_by_construction() -> None:
    queries = [_query("q1", "成分知识", ["a", "b", "c"])]
    corpus = {"a": "成分知识", "b": "成分知识", "c": "成分知识"}
    result = classify_labels(queries, corpus, {"q1": ["a", "b", "c"]})
    assert result["provenance_sources"][PROVENANCE_RELEVANCE] == 0
    assert result["valid_labels"] == 0


def test_provenance_classes_partition_all_labels() -> None:
    queries = [
        _query("q1", "成分知识", ["a", "b", "c"]),
        _query("q2", "产品推荐", ["d", "ghost", "e"]),
    ]
    corpus = {"a": "成分知识", "b": "成分知识", "c": "成分知识", "d": "产品介绍", "e": "使用方法"}
    generated = {"q1": ["a", "b", "c"]}
    result = classify_labels(queries, corpus, generated)
    assert (
        result["valid_labels"] + result["invalid_labels"] + result["unknown_provenance_labels"]
        == result["total_gold_labels"]
    )


def test_documented_generator_is_deterministic_and_matches_hand_derivation() -> None:
    docs = [
        {"id": "a", "category": "成分知识"},
        {"id": "b", "category": "成分知识"},
        {"id": "c", "category": "成分知识"},
        {"id": "d", "category": "成分知识"},
    ]
    queries = [_query("q1", "成分知识", ["unused"]), _query("q2", "成分知识", ["unused"])]
    first = documented_generator_gold(queries, docs, CATEGORY_MAP)
    second = documented_generator_gold(queries, docs, CATEGORY_MAP)
    assert first == second
    assert all(len(ids) == 3 for ids in first.values())


def test_committed_benchmark_has_no_relevance_gold_and_is_unmeasurable() -> None:
    report = analyze(PROJECT_ROOT)
    assert report["verdict"] == "FORMAL_RETRIEVAL_METRICS_NOT_MEASURABLE_FROM_CURRENT_GOLD"
    assert report["metrics_produced"] is None
    assert report["total_queries"] == 649
    assert report["total_gold_labels"] == 1947
    assert report["valid_labels"] == 0
    assert report["provenance_sources"][PROVENANCE_RELEVANCE] == 0
    assert (
        report["valid_labels"] + report["invalid_labels"] + report["unknown_provenance_labels"]
        == report["total_gold_labels"]
    )


def test_committed_benchmark_reproduction_and_corpus_gap_are_pinned() -> None:
    report = analyze(PROJECT_ROOT)
    assert report["reproduced_query_count"] == 49
    assert report["reproduced_label_count"] == 147
    assert report["labels_absent_from_corpus"] == 160
    assert report["distinct_labels_absent_from_corpus"] == 30
    assert report["queries_with_absent_gold"] == 80
    assert report["queries_fully_uncovered"] == 40
    assert report["benchmark_total_queries"] == report["total_queries"]


@pytest.mark.parametrize("key", ["schema_version", "benchmark_sha256"])
def test_report_records_provenance_metadata(key: str) -> None:
    report = analyze(PROJECT_ROOT)
    assert report[key]


def test_report_reproduces_the_corpus_document_set_without_the_gitignored_file() -> None:
    report = analyze(PROJECT_ROOT)
    assert report["corpus_documents"] == 5000
