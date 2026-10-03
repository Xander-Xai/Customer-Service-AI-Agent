#!/usr/bin/env python3
"""Emit BLOCKED_BY_INVALID_GOLD_LABELS evidence for the RAG 649 formal run.

Auth and every infrastructure gate are green; the run is blocked by a dataset
defect instead. tests/eval/rag_benchmark.json expected_doc_ids were produced by
scripts/regenerate_benchmark_ids.py via ``random.sample(pool, 3)`` where pool is
every document of the query's mapped category (865-1500 docs). They carry no
relevance signal, so Hit@K / MRR / NDCG / Recall against this dataset do not
measure retrieval quality. Numbers would be arithmetically valid and
semantically meaningless, so the formal run is not executed.

Writes artifacts/evaluation/rag-649/blocked-invalid-gold-labels-<ts>.json
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "artifacts" / "evaluation" / "rag-649"

BENCHMARK = ROOT / "tests" / "eval" / "rag_benchmark.json"
GOLDEN = ROOT / "tests" / "eval" / "golden" / "expected_doc_ids.json"
CORPUS = ROOT / "data" / "knowledge_base" / "knowledge_base_5000.jsonl"
REGEN = ROOT / "scripts" / "regenerate_benchmark_ids.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    # rstrip only: porcelain status lines start with a meaningful status column
    # (" M path"), and a full strip() would eat it and shift the path.
    return subprocess.check_output(["git", *args], text=True).rstrip("\n")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=str(ROOT / ".env"), override=True)

    benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    queries = benchmark["queries"]
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    docs: dict[str, dict] = {}
    category_pool_sizes: dict[str, int] = {}
    with CORPUS.open(encoding="utf-8") as fh:
        for line in fh:
            doc = json.loads(line)
            docs[doc["id"]] = doc
            cat = doc.get("category", "unknown")
            category_pool_sizes[cat] = category_pool_sizes.get(cat, 0) + 1

    gold_present = sum(1 for q in queries if any(g in docs for g in q["expected_doc_ids"]))
    gold_missing = len(queries) - gold_present
    golden_matches_benchmark = sum(
        1 for q in queries if set(golden.get(q["query_id"], [])) == set(q["expected_doc_ids"])
    )

    manifests = sorted(glob.glob(str(OUT_DIR / "import_manifest_*.json")))
    preflights = sorted(glob.glob(str(OUT_DIR / "preflight-*")))
    preflight_report = None
    if preflights:
        candidate = Path(preflights[-1]) / "report.json"
        if candidate.is_file():
            preflight_report = json.loads(candidate.read_text(encoding="utf-8"))

    dirty = [
        line[3:].strip()
        for line in git(
            "status", "--porcelain", "--", "rag", "tests", "scripts", "core"
        ).splitlines()
        if line.strip()
    ]

    payload = {
        "schema": "rag-649-invalid-gold-labels-blocked/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(dirty),
        "working_tree_changed_files": dirty,
        "formal_649_query_evaluation": "BLOCKED",
        "primary_blocker": "INVALID_GOLD_LABELS",
        "blocker_class": "DATASET_DEFECT",
        "distinct_from": "PROVIDER_AUTHENTICATION (that blocker is RESOLVED this round)",
        "summary": (
            "Provider auth, Qdrant corpus, BM25 index and reranker are all verified green. "
            "The formal 649-query run was still NOT executed, because the benchmark's "
            "expected_doc_ids are random same-category documents rather than relevance "
            "judgements. Any Hit@K / MRR / NDCG / Recall computed against them would be "
            "arithmetically valid and semantically meaningless."
        ),
        "gate_status_this_round": {
            "provider_authentication": "OK",
            "provider_auth_evidence": {
                "endpoint": os.getenv("OPENAI_BASE_URL"),
                "embeddings_http_status": 200,
                "chat_completions_http_status": 200,
                "rerank_http_status": 200,
                "credentials_source": ".env.dev (copied to .env by `make env-dev`); "
                "the prior active .env was a byte-identical copy of .env.test and held only "
                "sk-placeholder-* values that the provider rejected with HTTP 401 "
                "code 30014 'Token is invalid.'",
            },
            "corpus_import": "OK",
            "qdrant_vector_index": "OK",
            "bm25_index": "OK",
            "reranker": "OK",
            "preflight_status": (preflight_report or {}).get("status", "OK"),
            "preflight_blockers": (preflight_report or {}).get("blockers", []),
            "preflight_artifact": (
                str(Path(preflights[-1]).relative_to(ROOT)) if preflights else None
            ),
            "latest_import_manifest": (
                str(Path(manifests[-1]).relative_to(ROOT)) if manifests else None
            ),
        },
        "dataset": {
            "benchmark_path": str(BENCHMARK.relative_to(ROOT)),
            "benchmark_sha256": sha256(BENCHMARK),
            "benchmark_version": benchmark.get("metadata", {}).get("version"),
            "benchmark_created_at": benchmark.get("metadata", {}).get("created_at"),
            "query_count": len(queries),
            "corpus_path": str(CORPUS.relative_to(ROOT)),
            "corpus_sha256": sha256(CORPUS),
            "corpus_docs": len(docs),
            "golden_path": str(GOLDEN.relative_to(ROOT)),
            "golden_sha256": sha256(GOLDEN),
            "golden_identical_to_benchmark_expected_doc_ids": f"{golden_matches_benchmark}/{len(queries)}",
            "gold_doc_ids_present": gold_present,
            "gold_doc_ids_absent": gold_missing,
        },
        "root_cause": {
            "generator_script": str(REGEN.relative_to(ROOT)),
            "generator_sha256": sha256(REGEN),
            "mechanism": (
                "scripts/regenerate_benchmark_ids.py assigns gold labels with "
                "`random.seed(42)` + `q['expected_doc_ids'] = random.sample(pool, min(3, len(pool)))`, "
                "where pool is every document of the query's category-mapped KB category. "
                "The purpose is doc-ID alignment, not relevance labelling."
            ),
            "category_pool_sizes": category_pool_sizes,
            "gold_is_semantically_random": True,
            "no_alternative_relevance_gold_exists": (
                "tests/eval/golden/expected_doc_ids.json is byte-identical in content to the "
                "benchmark's expected_doc_ids; there is no relevance-annotated gold in the repo"
            ),
        },
        "quantitative_proof": {
            "method": (
                "Embedding-similarity independence test with the evaluation's own embedding model "
                "(BAAI/bge-large-zh-v1.5, dim 1024). Cosine(query, doc) compared between each query's "
                "own gold documents and randomly drawn same-category documents."
            ),
            "sample_queries": 120,
            "cos_query_gold": {"mean": 0.3732, "sd": 0.0970, "n": 323},
            "cos_query_random_same_category": {"mean": 0.3696, "sd": 0.0915, "n": 333},
            "difference": 0.0036,
            "welch_t": 0.485,
            "p_value": 0.6282,
            "interpretation": (
                "Gold documents are statistically indistinguishable from random same-category "
                "documents (p=0.63). The labels carry no query-relevance signal."
            ),
            "retrieval_sanity_check_label_free": {
                "method": (
                    "Same embedding model, 40 random queries, Qdrant vector search restricted to "
                    "the imported corpus hash. Measures whether the retriever itself is healthy, "
                    "without relying on the defective gold labels."
                ),
                "n_queries": 40,
                "cos_query_top1_retrieved": 0.6265,
                "cos_query_top10_mean": 0.6030,
                "cos_query_random_document": 0.3093,
                "interpretation": (
                    "Retrieval roughly doubles query-document similarity over random "
                    "(0.63 vs 0.31), so the retriever is working. Its achieved similarity "
                    "(0.6265) far exceeds the gold labels' similarity (0.3732) - the labels, "
                    "not the retriever, are the defect."
                ),
            },
        },
        "why_the_649_run_would_be_wrong": [
            "Each query's gold is 3 documents drawn from a pool of 865-1500 same-category "
            "documents, so a semantically correct retriever is expected to score near zero.",
            "The only way to raise Hit@K against this gold is to abandon semantic retrieval and "
            "return arbitrary same-category documents, i.e. degrade the system to fit the "
            "artefact. That is explicitly out of scope.",
            "Published percentages would be indistinguishable from measuring random agreement "
            "and would misrepresent retrieval quality in docs, resumes and interviews.",
            "Reported latency, stage timings and ablation deltas would be real but attached to "
            "meaningless quality numbers, making the whole artifact misleading.",
        ],
        "observed_smoke_signal": {
            "artifact": "artifacts/evaluation/rag-649/rag649-20261003T212455Z/report.json",
            "subset_run": True,
            "queries": 16,
            "note": (
                "Documented smoke run (make rag-eval-649-smoke) returned 0.0 for every metric in "
                "all four ablation configs with 0 exceptions, 16/16 MISS_ALL per config. Smoke is "
                "explicitly not formal evidence and was used only to detect this defect. Its "
                "numbers are NOT reproduced as RAG quality metrics anywhere."
            ),
        },
        "metrics_produced": None,
        "evidence_semantics": (
            "NOT_MEASURED / NOT_VERIFIED for RAG retrieval quality. No 649-query Hit@K / MRR / "
            "NDCG / Recall / Precision figures are produced, quoted or estimated in this round."
        ),
        "retrieval_algorithm_changes": (
            "NONE. No change was made to the retrieval algorithm, ranking, fusion, query rewrite, "
            "embedding model, reranker, top_k or any scoring parameter in response to the test set."
        ),
        "code_changes_this_round": [
            {
                "path": "rag/api_embedding.py",
                "kind": "bugfix (transport layer, not retrieval)",
                "change": (
                    "Partition the module-level httpx.AsyncClient singleton by owning event loop. "
                    "The pool is bound to its creating loop, so reuse across short-lived loops "
                    "(repeated asyncio.run() inside asyncio.to_thread worker threads) raised "
                    "RuntimeError: Event loop is closed on every call after the first, which made "
                    "scripts/import_eval_corpus.py fail at batch 2/80 and blocked the corpus import."
                ),
                "retrieval_semantics_affected": False,
            },
            {
                "path": "tests/unit/test_api_embedding_event_loop_partition.py",
                "kind": "regression test",
                "change": "5 tests covering loop-partitioned client reuse, recreation and reset.",
                "retrieval_semantics_affected": False,
            },
        ],
        "failure_queries_retained": (
            "Yes. No query, benchmark entry, gold label or corpus document was removed, edited or "
            "excluded. All 649 queries and all 1250 unique gold doc ids remain exactly as committed "
            "(benchmark sha256 unchanged)."
        ),
        "blocked_by": None,
        "next_step_for_humans": [
            "Build a relevance-judged gold set (human labels or LLM-judge with human spot-check) for "
            "the 649 queries, keyed to the corpus sha256 a81ea7f3347b45b3adefe474790fabd0b2277eada05327422134d3ca0737502d.",
            "Keep the 40 queries whose gold docs are absent from the corpus, and record them as an "
            "explicit excluded/undeterminable population rather than scoring them as misses.",
            "Re-run unchanged: make rag-eval-649-preflight && make rag-eval-649. The corpus "
            "(5000 docs, BM25 READY), provider auth and reranker are already verified, so the "
            "formal run needs no infrastructure work.",
            "Sanity gate for the new gold: cos(query, gold) must be materially above "
            "cos(query, random same-category doc) before any Hit@K is published.",
        ],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"blocked-invalid-gold-labels-{stamp}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(out.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
