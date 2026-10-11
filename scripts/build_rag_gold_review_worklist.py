#!/usr/bin/env python3
"""生成 RAG 人工标注工作清单（把 649 benchmark 变成可人工核验的 gold）。

它解决什么
----------
``tests/eval/rag_benchmark.json`` 的 649 条 ``expected_doc_ids`` **不是相关度判定**
（``scripts/rag_gold_label_provenance.py`` 结论：``DATASET_DEFECT``）。本脚本把每条
query 变成一条 **DRAFT_UNVERIFIED** 的 ``rag-gold-label/v1`` 记录，并附上：

* 每个候选 gold 的**语料存在性**（``in_corpus``）—— 直接修掉"部分 gold 不在语料里"；
* 候选文档的标题/类别，供人快速判断相关性；
* 语料 hash 与版本，锁定标注对应的数据快照；
* 无法判定的条目（全部 gold 缺失）显式落 **UNDETERMINABLE**，**不**自动判为相关。

输出分三类（全部保留，不丢弃任何原始 query）：

============================  ==========================================
类别                          条件
============================  ==========================================
``DRAFT_UNVERIFIED``          至少 1 个候选 gold 存在于语料 -> 待人工判定
``UNDETERMINABLE``            全部候选 gold 都不在语料中 -> 无法判定
（不产生 JUDGED）             人工判定只能由人来做，本脚本不代劳
============================  ==========================================

用法::

    python3 scripts/build_rag_gold_review_worklist.py
    python3 scripts/build_rag_gold_review_worklist.py --limit 50   # 先做一小批
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from gold_label_contract import (  # noqa: E402
    LABEL_STATUS_DRAFT,
    LABEL_STATUS_UNDETERMINABLE,
    METHOD_CATEGORY_RANDOM,
    SCHEMA_VERSION,
    corpus_sha256,
    validate_records,
)

DEFAULT_BENCHMARK = REPO_ROOT / "tests" / "eval" / "rag_benchmark.json"
DEFAULT_CORPUS = REPO_ROOT / "data" / "knowledge_base" / "knowledge_base_5000.jsonl"
DEFAULT_OUT = REPO_ROOT / "tests" / "eval" / "gold_labels" / "review_worklist.jsonl"
CORPUS_VERSION = "knowledge_base_5000@2026-10"
ANNOTATOR = "worklist_generator_v1"
#: 记录**来源**的方法标签。用 category_random_match 是**如实描述**：benchmark 的
#: expected_doc_ids 本来就是"同类别随机抽样"得来的（见 rag_gold_label_provenance.py）。
#: 契约允许它处于 DRAFT / UNDETERMINABLE，只禁止它被标成 JUDGED 或携带 grade ——
#: 那正是"随机匹配不是相关度判定"这条规则的落点。
#: 用 llm_suggested 反而是错的描述，且会被契约第 7 条拒绝。
EXCLUDED_ALL_MISSING = "all candidate gold documents are absent from the evaluated corpus"


def _load_corpus(path: Path) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if line:
                doc = json.loads(line)
                by_id[doc["id"]] = doc
    return by_id


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)

    corpus_path = Path(args.corpus)
    corpus = _load_corpus(corpus_path)
    corpus_ids = set(corpus)
    corpus_hash = corpus_sha256(corpus_path)

    benchmark = json.loads(Path(args.benchmark).read_text(encoding="utf-8"))
    queries = benchmark["queries"]
    if args.limit is not None:
        queries = queries[: args.limit]

    records: list[dict] = []
    stats: Counter = Counter()
    missing_gold_total = 0
    fully_missing_queries = 0

    for query in queries:
        expected = list(query.get("expected_doc_ids") or [])
        present = [d for d in expected if d in corpus_ids]
        missing = [d for d in expected if d not in corpus_ids]
        missing_gold_total += len(missing)

        if not present:
            fully_missing_queries += 1
            records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "query_id": query["query_id"],
                    "query": query["query"],
                    "corpus_version": CORPUS_VERSION,
                    "corpus_hash": corpus_hash,
                    "doc_id": None,
                    "evidence_span": "",
                    "relevance_grade": None,
                    "annotator": ANNOTATOR,
                    "annotation_method": METHOD_CATEGORY_RANDOM,
                    "reviewed_at": None,
                    "label_status": LABEL_STATUS_UNDETERMINABLE,
                    "exclusion_reason": (
                        f"{EXCLUDED_ALL_MISSING}；候选 {missing} 均不在 {len(corpus_ids)} 条语料中"
                    ),
                    "derivation_rule": None,
                }
            )
            stats["UNDETERMINABLE"] += 1
            continue

        for doc_id in present:
            doc = corpus[doc_id]
            records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "query_id": query["query_id"],
                    "query": query["query"],
                    "corpus_version": CORPUS_VERSION,
                    "corpus_hash": corpus_hash,
                    "doc_id": doc_id,
                    "evidence_span": "",
                    "relevance_grade": None,
                    "annotator": ANNOTATOR,
                    "annotation_method": METHOD_CATEGORY_RANDOM,
                    "reviewed_at": None,
                    "label_status": LABEL_STATUS_DRAFT,
                    "exclusion_reason": None,
                    "derivation_rule": None,
                    # 供人工判定的上下文（不是契约字段，读取方按需忽略）
                    "review_context": {
                        "doc_title": doc.get("title", ""),
                        "doc_category": doc.get("category", ""),
                        "doc_excerpt": (doc.get("content") or "")[:200],
                        "benchmark_category": query.get("category"),
                        "scene": query.get("scene"),
                        "note": (
                            "benchmark 的 expected_doc_ids 来自同类别随机抽样，不是相关度判定；"
                            "请人工读完 doc_excerpt 后填写 evidence_span + relevance_grade(0-3)，"
                            "并把 label_status 改为 JUDGED、annotation_method 改为 human。"
                        ),
                    },
                }
            )
            stats["DRAFT_UNVERIFIED"] += 1

    # review_context 不是契约字段 -> 校验前剥离，避免污染契约（结构上仍写进文件供人使用）
    contract_errors = validate_records(
        [{k: v for k, v in r.items() if k != "review_context"} for r in records],
        corpus_ids=corpus_ids,
        expected_corpus_hash=corpus_hash,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )

    print(
        f"wrote {len(records)} worklist records -> "
        f"{out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}"
    )
    print("\n标注状态分布：")
    for status, count in sorted(stats.items()):
        print(f"  {status:20s} {count}")
    print("\n语料覆盖（这是「gold 不在语料里」的实际规模）：")
    print(f"  corpus docs                     {len(corpus_ids)}")
    print(f"  benchmark queries               {len(queries)}")
    print(f"  完全无有效 gold 的 query        {fully_missing_queries}")
    print(f"  语料中缺失的 gold 引用总数      {missing_gold_total}")

    if contract_errors:
        print(
            f"\n❌ worklist 未通过 rag-gold-label/v1 校验（{len(contract_errors)} 项）：",
            file=sys.stderr,
        )
        for err in contract_errors[:5]:
            print(f"   - {err}", file=sys.stderr)
        return 1
    print("\n✅ worklist 通过 rag-gold-label/v1 契约校验")
    print("   ⚠️ 全部是 DRAFT_UNVERIFIED / UNDETERMINABLE —— 没有任何一条是 JUDGED。")
    print("      本脚本**不**代做相关度判定；人工判定入口见脚本 docstring。")
    if args.summary:
        print(
            json.dumps(
                {"stats": dict(stats), "missing_gold": missing_gold_total},
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
