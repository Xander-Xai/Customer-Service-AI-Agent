#!/usr/bin/env python3
"""Static provenance audit of the RAG 649 benchmark gold labels (Issue #99).

Why this module exists
----------------------
The formal 649-query RAG evaluation reports Hit@K / Precision@K / Recall@K /
MRR / NDCG@K. Every one of those numbers is computed against
``tests/eval/rag_benchmark.json``'s ``expected_doc_ids``. If those ids are not
relevance judgements, the numbers are *arithmetically valid and semantically
meaningless* — a worse failure mode than an honest ``NOT_VERIFIED``, because the
output wears the shape of formal evidence.

This module answers, from static repository facts only and with no provider,
LLM, embedding or reranker call:

1. Is there a documented in-repo generator for ``expected_doc_ids``?
2. Do the committed labels reproduce from that generator?
3. For the labels that do not, is there any recorded relevance provenance?
4. Do all gold ids exist in the evaluation corpus at all?

It intentionally does **not** produce Hit@K / MRR / NDCG / Recall, and it never
modifies the benchmark, the corpus or the retrieval algorithm.

Provenance classes (per gold label)
-----------------------------------
- ``relevance_annotation`` — a recorded human/LLM relevance judgement. None
  exists in this repository as of the audited commit.
- ``documented_generator_random_same_category`` — the label reproduces exactly
  from ``scripts/regenerate_benchmark_ids.py`` (``random.seed(42)`` +
  ``random.sample(pool, 3)`` over the query's category-mapped documents). Its
  purpose is doc-id alignment, not relevance.
- ``absent_from_corpus`` — the gold id is not present in
  ``data/knowledge_base/knowledge_base_5000.jsonl``, so it cannot be retrieved
  over the evaluation corpus.
- ``unknown_provenance`` — the id exists in the corpus but no generator or
  annotation recorded in the repository explains why it is a gold document.

Usage
-----
    python3 scripts/rag_gold_label_provenance.py
    python3 scripts/rag_gold_label_provenance.py --stdout

Writes ``artifacts/evaluation/rag-gold-provenance/<ts>/report.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = "rag-gold-label-provenance/v1"
_VERDICT = "FORMAL_RETRIEVAL_METRICS_NOT_MEASURABLE_FROM_CURRENT_GOLD"

PROVENANCE_RELEVANCE = "relevance_annotation"
PROVENANCE_GENERATOR = "documented_generator_random_same_category"
PROVENANCE_ABSENT = "absent_from_corpus"
PROVENANCE_UNKNOWN = "unknown_provenance"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(root), text=True, stderr=subprocess.DEVNULL
    ).rstrip("\n")


def documented_generator_gold(
    queries: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    category_map: dict[str, str],
) -> dict[str, list[str]]:
    """Reproduce ``scripts/regenerate_benchmark_ids.py`` for every query.

    Mirrors the producer exactly: build the per-benchmark-category candidate
    pool (falling back to *all* documents for an unmapped category), then draw
    three ids per query with a single ``random.seed(42)`` stream in query order.
    A dedicated ``random.Random`` instance keeps the global RNG untouched.
    """
    docs_by_category: dict[str, list[str]] = defaultdict(list)
    for doc in docs:
        docs_by_category[doc.get("category", "unknown")].append(doc["id"])
    all_ids = [doc["id"] for doc in docs]
    candidates = {
        bench_cat: docs_by_category.get(kb_cat, all_ids)
        for bench_cat, kb_cat in category_map.items()
    }

    rng = random.Random(42)
    generated: dict[str, list[str]] = {}
    for query in queries:
        pool = candidates.get(query.get("category", ""), all_ids)
        generated[query["query_id"]] = rng.sample(pool, min(3, len(pool)))
    return generated


def classify_labels(
    queries: list[dict[str, Any]],
    corpus_categories: dict[str, str],
    generated: dict[str, list[str]],
) -> dict[str, Any]:
    """Classify every gold label by the provenance evidence recorded in-repo."""
    counts = {
        PROVENANCE_RELEVANCE: 0,
        PROVENANCE_GENERATOR: 0,
        PROVENANCE_ABSENT: 0,
        PROVENANCE_UNKNOWN: 0,
    }
    examples: dict[str, dict[str, Any]] = {}
    reproduced_queries: list[str] = []
    queries_with_absent: list[str] = []
    queries_fully_absent: list[str] = []

    for query in queries:
        qid = query["query_id"]
        gold = list(query["expected_doc_ids"])
        reproduced = generated.get(qid) == gold
        if reproduced:
            reproduced_queries.append(qid)

        absent_here = 0
        for doc_id in gold:
            if doc_id not in corpus_categories:
                label_class = PROVENANCE_ABSENT
                absent_here += 1
            elif reproduced:
                label_class = PROVENANCE_GENERATOR
            else:
                label_class = PROVENANCE_UNKNOWN
            counts[label_class] += 1
            if label_class not in examples:
                examples[label_class] = {
                    "query_id": qid,
                    "query_category": query.get("category"),
                    "expected_doc_ids": gold,
                }

        if absent_here:
            queries_with_absent.append(qid)
            if absent_here == len(gold):
                queries_fully_absent.append(qid)

    total_labels = sum(len(query["expected_doc_ids"]) for query in queries)
    valid = counts[PROVENANCE_RELEVANCE]
    invalid = counts[PROVENANCE_GENERATOR] + counts[PROVENANCE_ABSENT]
    unknown = counts[PROVENANCE_UNKNOWN]
    if valid + invalid + unknown != total_labels:
        raise AssertionError("provenance classes do not partition the gold labels")

    return {
        "total_queries": len(queries),
        "total_gold_labels": total_labels,
        "valid_labels": valid,
        "invalid_labels": invalid,
        "unknown_provenance_labels": unknown,
        "provenance_sources": counts,
        "reproduced_query_count": len(reproduced_queries),
        "reproduced_label_count": counts[PROVENANCE_GENERATOR],
        "queries_with_absent_gold": len(queries_with_absent),
        "queries_fully_uncovered": len(queries_fully_absent),
        "labels_absent_from_corpus": counts[PROVENANCE_ABSENT],
        "distinct_labels_absent_from_corpus": len(
            {
                doc_id
                for query in queries
                for doc_id in query["expected_doc_ids"]
                if doc_id not in corpus_categories
            }
        ),
        "examples": examples,
        "reproduced_query_examples": reproduced_queries[:5],
        "fully_uncovered_query_examples": queries_fully_absent[:5],
        "verdict": _VERDICT,
    }


def _tracked_changes(root: Path, exclude_prefix: str) -> list[str]:
    """Tracked/untracked changes, excluding the tool's own artifact directory."""
    out = _git(root, "status", "--porcelain")
    changes = []
    for line in out.splitlines():
        path = line[3:].strip()
        if path.startswith(exclude_prefix):
            continue
        changes.append(line)
    return changes


def analyze(root: Path) -> dict[str, Any]:
    """Run the full static audit and return the serialisable report payload."""
    sys.path.insert(0, str(root))
    from scripts.generate_knowledge_base import generate
    from scripts.regenerate_benchmark_ids import CATEGORY_MAP

    benchmark_path = root / "tests" / "eval" / "rag_benchmark.json"
    corpus_path = root / "data" / "knowledge_base" / "knowledge_base_5000.jsonl"
    out_prefix = str(Path("artifacts") / "evaluation" / "rag-gold-provenance")

    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    queries = benchmark["queries"]
    docs = generate()
    corpus_categories = {doc["id"]: doc.get("category", "unknown") for doc in docs}

    generated = documented_generator_gold(queries, docs, CATEGORY_MAP)
    classification = classify_labels(queries, corpus_categories, generated)

    changed = _tracked_changes(root, out_prefix)
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tested_git_sha": _git(root, "rev-parse", "HEAD"),
        "dirty_tree": bool(changed),
        "dirty_tree_scope": "repository changes excluding the tool's own artifact directory",
        "working_tree_changed_files": changed,
        "benchmark_path": str(benchmark_path.relative_to(root)),
        "benchmark_sha256": _sha256(benchmark_path),
        "benchmark_total_queries": benchmark.get("metadata", {}).get("total_queries"),
        "corpus_path": str(corpus_path.relative_to(root)),
        "corpus_sha256": _sha256(corpus_path) if corpus_path.is_file() else None,
        "corpus_documents": len(corpus_categories),
        "corpus_source": (
            "scripts.generate_knowledge_base.generate() — the corpus .jsonl is generated "
            "and gitignored, so the document set is reproduced deterministically in memory"
        ),
        "documented_generator": "scripts/regenerate_benchmark_ids.py",
        "documented_generator_sha256": _sha256(root / "scripts" / "regenerate_benchmark_ids.py"),
        "metrics_produced": None,
        "evidence_semantics": (
            "NOT_MEASURED / NOT_VERIFIED: this audit produces no Hit@K, Precision@K, "
            "Recall@K, MRR or NDCG. It only establishes whether the shipped gold labels "
            "can support them."
        ),
        "notes": [
            "Category membership is not relevance. A same-category document is not a "
            "relevance judgement.",
            "The documented generator (scripts/regenerate_benchmark_ids.py) samples three "
            "random same-category documents per query with random.seed(42); its purpose is "
            "doc-id alignment, not labelling.",
            "A label that does not reproduce from the generator still has no recorded "
            "relevance provenance anywhere in the repository.",
            "tests/eval/golden/expected_doc_ids.json is a mechanical copy of the "
            "benchmark's expected_doc_ids, not an independent annotation.",
            "No query, label or corpus document was modified by this audit.",
        ],
    }
    payload.update(classification)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument(
        "--stdout", action="store_true", help="print the report instead of writing it"
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    payload = analyze(root)

    if args.stdout:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    out_dir = root / "artifacts" / "evaluation" / "rag-gold-provenance"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = out_dir / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "report.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(out.relative_to(root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
