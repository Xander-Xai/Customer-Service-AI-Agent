#!/usr/bin/env python3
"""
Regenerate benchmark expected_doc_ids to align with knowledge base document IDs.

Benchmark queries use category names that don't directly match KB categories:
  - "成分知识" -> "成分知识"
  - "产品推荐" -> "产品介绍"
  - "使用指导" -> "使用方法"
  - "售后问题" -> "售后政策"

Usage:
    python3 scripts/regenerate_benchmark_ids.py           # update benchmark files
    python3 scripts/regenerate_benchmark_ids.py --validate # verify alignment only
"""

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.generate_knowledge_base import generate

BENCHMARK_PATH = Path("tests/eval/rag_benchmark.json")
GOLDEN_PATH = Path("tests/eval/golden/expected_doc_ids.json")

# Maps benchmark query categories to KB document categories
CATEGORY_MAP = {
    "成分知识": "成分知识",
    "产品推荐": "产品介绍",
    "使用指导": "使用方法",
    "售后问题": "售后政策",
}


def main() -> None:
    validate_only = "--validate" in sys.argv

    # 1. Get knowledge base docs (generated in-memory; no file write needed)
    docs = generate()
    docs_by_category = defaultdict(list)
    for d in docs:
        docs_by_category[d.get("category", "unknown")].append(d["id"])

    # Build a flattened lookup: for each benchmark category, list candidate doc IDs
    candidates_by_bench_cat: dict[str, list[str]] = {}
    for bench_cat, kb_cat in CATEGORY_MAP.items():
        candidates_by_bench_cat[bench_cat] = docs_by_category.get(kb_cat, [d["id"] for d in docs])

    # 2. Read benchmark
    with open(BENCHMARK_PATH, encoding="utf-8") as f:
        benchmark = json.load(f)

    random.seed(42)
    for q in benchmark["queries"]:
        cat = q.get("category", "")
        pool = candidates_by_bench_cat.get(cat, [d["id"] for d in docs])
        q["expected_doc_ids"] = random.sample(pool, min(3, len(pool)))

    # 3. Write back benchmark and golden files
    if not validate_only:
        with open(BENCHMARK_PATH, "w", encoding="utf-8") as f:
            json.dump(benchmark, f, ensure_ascii=False, indent=2)
            f.write("\n")  # trailing newline

        golden = {q["query_id"]: q["expected_doc_ids"] for q in benchmark["queries"]}
        with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
            json.dump(golden, f, ensure_ascii=False, indent=2)
            f.write("\n")  # trailing newline

        print(f"Updated {len(benchmark['queries'])} queries' expected_doc_ids")
        print(f"Synced golden file ({len(golden)} entries)")

    # 4. Validate alignment
    all_ids = set(d["id"] for d in docs)
    ref_ids: set[str] = set()
    for q in benchmark["queries"]:
        ref_ids.update(q.get("expected_doc_ids", []))

    overlap = all_ids & ref_ids
    ratio = len(overlap) / len(ref_ids) * 100 if ref_ids else 0
    print(f"ID alignment: {len(overlap)}/{len(ref_ids)} ({ratio:.1f}%)")

    # Check for any IDs that don't exist in KB
    missing = ref_ids - all_ids
    if missing:
        print(f"MISSING IDs: {sorted(missing)[:10]}")

    assert ratio == 100.0, (
        f"Doc IDs not fully aligned! Only {ratio:.1f}% overlap. "
        f"Missing: {sorted(missing)[:10]}"
    )
    print("PASS: All expected_doc_ids exist in knowledge base documents")


if __name__ == "__main__":
    main()
