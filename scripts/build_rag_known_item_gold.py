#!/usr/bin/env python3
"""构建 **已知项（known-item）** gold 标注集 —— 唯一一种不需要人工案例判定的合法 gold。

问题
----
``tests/eval/rag_benchmark.json`` 的 649 条 gold **不是相关度判定**
（``scripts/rag_gold_label_provenance.py`` 的结论：``DATASET_DEFECT``）。在没有
人工标注之前，任何 Hit@K / MRR / NDCG 都不能作为正式检索质量结论。

但"没有正式结论"不等于"什么都不能测"。本脚本构建一类**可机械验证**的 gold：

    query  := 目标文档自己的标题（逐字）
    gold   := 该文档
    相关度 := **由构造保证**，不是判断得出的

任何评审者只要打开文档就能核验查询确实来自它。因此它度量的是
**索引的词法可检索性**（retrieval stack 有没有正常把语料装进去、BM25 通道
能不能命中），这是一个真实的正确性属性 —— 而它**不是**"搜索质量"。
两者在 artifact 与报告里都以 ``population=CONSTRUCTED`` 与
``population=JUDGED`` 分开呈现，**永不合并**。

为什么用"标题"而不是"正文里的任意句子"
--------------------------------------
1. 标题是文档的作者给出的**主题标识**，用它当查询在语义上最接近"用户在找这个主题"；
2. 逐字可核验（``evidence_span == title``），不存在"这句话到底算不算相关"的裁量空间；
3. 正文句子在同一标题下大量重复（本语料高度冗余），会把答案变成"检索到任意同主题
   文档" —— 那既不是 known-item，也不是 in-set 相关，两头不靠。

语料的已知缺陷（必须随结果一起报告）
------------------------------------
本语料 ``source: "synthetic"``，且**大量文档共享同一标题**。因此：
* 只挑选**标题唯一**的文档，保证 known-item 是确定的（标题重复时无法定义"唯一正确项"）；
* 结果表里显式给出 ``unique_title_docs`` / ``duplicate_title_docs`` 分布。

输出
----
``tests/eval/gold_labels/known_item_gold.jsonl``（``rag-gold-label/v1`` 契约）。
每条都经过契约校验器（含 doc_id 语料存在性 + corpus_hash 一致性）。

用法::

    python3 scripts/build_rag_known_item_gold.py                    # 生成
    python3 scripts/build_rag_known_item_gold.py --limit 200 --summary
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from gold_label_contract import (  # noqa: E402
    LABEL_STATUS_CONSTRUCTED,
    METHOD_DERIVED,
    SCHEMA_VERSION,
    corpus_sha256,
    validate_records,
)

DEFAULT_CORPUS = REPO_ROOT / "data" / "knowledge_base" / "knowledge_base_5000.jsonl"
DEFAULT_OUT = REPO_ROOT / "tests" / "eval" / "gold_labels" / "known_item_gold.jsonl"
CORPUS_VERSION = "knowledge_base_5000@2026-10"
DERIVATION_RULE = "query := document.title (verbatim); relevance guaranteed by construction"
ANNOTATOR = "derive_from_title_v1"


def _load_corpus(path: Path) -> list[dict]:
    docs: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj.get("id"), str) or not obj["id"]:
                raise ValueError(f"{path}:{lineno}: corpus row missing a string `id`")
            docs.append(obj)
    return docs


def build(corpus_path: Path, limit: int | None) -> tuple[list[dict], dict]:
    docs = _load_corpus(corpus_path)
    corpus_hash = corpus_sha256(corpus_path)
    corpus_ids = {d["id"] for d in docs}

    by_title: dict[str, list[str]] = defaultdict(list)
    for doc in docs:
        title = (doc.get("title") or "").strip()
        if title:
            by_title[title].append(doc["id"])

    unique_titles = {t: ids[0] for t, ids in by_title.items() if len(ids) == 1}
    duplicate_title_docs = sum(len(ids) for ids in by_title.values() if len(ids) > 1)

    # 稳定顺序：按 doc_id 排序，保证同样的语料永远生成同样的 gold 集。
    selected = sorted(unique_titles.items(), key=lambda kv: kv[1])
    if limit is not None:
        selected = selected[:limit]

    records: list[dict] = []
    for title, doc_id in selected:
        records.append(
            {
                "schema_version": SCHEMA_VERSION,
                "query_id": f"known_item::{doc_id}",
                "query": title,
                "corpus_version": CORPUS_VERSION,
                "corpus_hash": corpus_hash,
                "doc_id": doc_id,
                "evidence_span": title,
                "relevance_grade": 3,
                "annotator": ANNOTATOR,
                "annotation_method": METHOD_DERIVED,
                "reviewed_at": None,
                "label_status": LABEL_STATUS_CONSTRUCTED,
                "exclusion_reason": None,
                "derivation_rule": DERIVATION_RULE,
            }
        )

    stats = {
        "corpus_path": str(corpus_path),
        "corpus_hash": corpus_hash,
        "corpus_docs": len(docs),
        "distinct_titles": len(by_title),
        "unique_title_docs": len(unique_titles),
        "duplicate_title_docs": duplicate_title_docs,
        "selected": len(records),
        "corpus_ids_checked": len(corpus_ids),
    }
    return records, stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)

    corpus_path = Path(args.corpus)
    records, stats = build(corpus_path, args.limit)

    errors = validate_records(records, corpus_ids=set(), expected_corpus_hash=stats["corpus_hash"])
    # 用真实 corpus_ids 再校验一次存在性（上面的空集只是占位）。
    real_ids = {
        json.loads(line)["id"]
        for line in corpus_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    errors = validate_records(
        records, corpus_ids=real_ids, expected_corpus_hash=stats["corpus_hash"]
    )
    if errors:
        print(f"❌ 生成的 gold 未通过契约校验（{len(errors)} 项）：", file=sys.stderr)
        for err in errors[:10]:
            print(f"   - {err}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )

    print(
        f"wrote {len(records)} constructed gold records -> "
        f"{out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}"
    )
    print("\n语料与构造统计（必须随任何指标一起报告）：")
    for key, value in stats.items():
        print(f"  {key:24s} {value}")
    print("\n⚠️ 这些是 CONSTRUCTED gold（由构造保证相关），**不是**人工相关度判定。")
    print("   它们度量索引的词法可检索性，不是真实查询的搜索质量。")
    print("   真实查询的正式指标仍需人工标注：make rag-gold-review")
    if args.summary:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
