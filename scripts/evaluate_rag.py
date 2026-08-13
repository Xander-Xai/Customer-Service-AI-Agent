#!/usr/bin/env python3
"""
RAG 检索质量评估脚本（v6.1 升级版）
基于 500+ 基准查询，使用 expected_doc_ids 精确评估。

评估指标：Recall@3, Precision@3, MRR（Mean Reciprocal Rank）
分组维度：difficulty（easy/medium/hard），category（成分知识/产品推荐/使用指导/售后问题/投诉处理）

用法：
    python3 scripts/evaluate_rag.py

输出：
    控制台报告 + reports/rag_eval_{date}.json
"""

import asyncio
import contextlib
import json
import math
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

# 添加项目根目录到 path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---- 延迟导入 ----
QDRANT_AVAILABLE = False
CosmeticsKnowledgeBase = None


def _resolve_kb():
    """尝试导入 QdrantKnowledgeBase"""
    global CosmeticsKnowledgeBase, QDRANT_AVAILABLE

    try:
        from rag.qdrant_knowledge_base import QdrantKnowledgeBase

        CosmeticsKnowledgeBase = QdrantKnowledgeBase
        QDRANT_AVAILABLE = True
    except ImportError:
        print("  [ERROR] QdrantKnowledgeBase 导入失败")
        sys.exit(1)
    except Exception as e:
        print(f"  [FATAL] 知识库初始化失败: {e}")
        sys.exit(1)


def _resolve_seed_data(kb):
    """延迟导入种子数据函数并执行初始化"""
    from rag.seed_data import (
        seed_complaint_knowledge,
        seed_faq,
        seed_product_knowledge,
        seed_supplementary_data,
        seed_tech_support,
    )

    kb.seed_if_empty("product_knowledge", seed_product_knowledge)
    kb.seed_if_empty("faq", seed_faq)
    kb.seed_if_empty("tech_support", seed_tech_support)
    kb.seed_if_empty("complaint_knowledge", seed_complaint_knowledge)
    with contextlib.suppress(Exception):
        seed_supplementary_data(kb)


def load_benchmark() -> dict:
    """从 tests/eval/rag_benchmark.json 加载评估数据集"""
    bm_path = PROJECT_ROOT / "tests" / "eval" / "rag_benchmark.json"
    if not bm_path.exists():
        print(f"  [ERROR] 基准文件不存在: {bm_path}")
        sys.exit(1)
    with open(bm_path, encoding="utf-8") as f:
        return json.load(f)


def compute_metrics(
    retrieved: list[dict[str, Any]],
    expected_ids: list[str],
    k: int = 3,
) -> dict[str, float]:
    """
    计算单次查询的评估指标。

    Args:
        retrieved: KB 返回的 top-K 检索结果列表，每条含 {"id": str, ...}
        expected_ids: 金标准中期望的文档 ID 列表
        k: Top-K 值

    Returns:
        {recall_at_k, precision_at_k, mrr, ndcg_at_k, hit, first_relevant_rank}
    """
    top_k = retrieved[:k]
    expected_set = set(expected_ids)

    # 命中统计
    precision_hits = 0
    hit = False
    first_relevant_rank = 0

    for rank, doc in enumerate(top_k, 1):
        doc_id = doc.get("id", "")
        if doc_id in expected_set:
            precision_hits += 1
            if not hit:
                hit = True
                first_relevant_rank = rank

    # Precision@K: 前 K 条中相关文档的比例
    precision_at_k = precision_hits / k

    # Recall@K: 前 K 条中召回的相关文档占所有相关文档的比例
    recall_at_k = precision_hits / max(len(expected_set), 1)

    # MRR: 第一个相关文档的倒数排名
    mrr = 1.0 / first_relevant_rank if first_relevant_rank > 0 else 0.0

    # NDCG@K: 归一化折损累计增益
    dcg = 0.0
    for rank, doc in enumerate(top_k, 1):
        doc_id = doc.get("id", "")
        rel = 1.0 if doc_id in expected_set else 0.0
        dcg += rel / math.log2(rank + 1)
    n_relevant = min(len(expected_set), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, n_relevant + 1))
    ndcg_at_k = dcg / idcg if idcg > 0 else 0.0

    return {
        "recall_at_k": round(recall_at_k, 4),
        "precision_at_k": round(precision_at_k, 4),
        "mrr": round(mrr, 4),
        "ndcg_at_k": round(ndcg_at_k, 4),
        "hit": hit,
        "first_relevant_rank": first_relevant_rank,
    }


async def evaluate_rag() -> dict:
    """执行 RAG 检索质量评估"""

    print("=" * 76)
    print("  RAG 检索质量评估报告 (v6.1)")
    print("  500 基准查询 | 5 类评估 | 3 级难度 | 4 维度指标")
    print("=" * 76)
    print()

    # 0. 解析 KB
    _resolve_kb()
    kb_name = "QdrantKnowledgeBase"

    # 1. 加载基准数据
    benchmark = load_benchmark()
    meta = benchmark["metadata"]
    queries = benchmark["queries"]

    print("[1/5] 加载基准数据")
    print(f"      版本: {meta['version']}")
    print(f"      查询数: {meta['total_queries']}")
    print(f"      分类: {json.dumps(meta['categories'], ensure_ascii=False)}")
    print(f"      难度: {json.dumps(meta['difficulty'], ensure_ascii=False)}")
    print()

    # 2. 初始化知识库
    print(f"[2/5] 初始化知识库 ({kb_name})...")
    try:
        kb = CosmeticsKnowledgeBase(host="localhost", port=6333)
    except Exception:
        print("  [FATAL] Qdrant 连接失败")
        sys.exit(1)
    _resolve_seed_data(kb)

    pk_count = kb.get_collection_count("product_knowledge")
    faq_count = kb.get_collection_count("faq")
    ts_count = kb.get_collection_count("tech_support")
    ck_count = kb.get_collection_count("complaint_knowledge")
    print(f"      product_knowledge: {pk_count} docs")
    print(f"      faq: {faq_count} docs")
    print(f"      tech_support: {ts_count} docs")
    print(f"      complaint_knowledge: {ck_count} docs")
    print(f"      总计: {pk_count + faq_count + ts_count + ck_count} docs")
    print()

    # 3. 执行检索评估
    total = len(queries)
    print(f"[3/5] 执行检索评估（共 {total} 条查询）...")
    print()

    collections = ["product_knowledge", "faq", "tech_support", "complaint_knowledge"]
    K = 3

    results: list[dict[str, Any]] = []
    progress_interval = max(1, total // 20)  # 每 5% 输出一次进度

    t_start = time.monotonic()
    for idx, case in enumerate(queries, 1):
        query_text = case["query"]
        expected_ids = case["expected_doc_ids"]
        category = case["category"]
        difficulty = case["difficulty"]
        query_id = case["query_id"]

        retrieved = await kb.query_multiple(collections, query_text, n_results=K)
        metrics = compute_metrics(retrieved, expected_ids, k=K)

        metrics.update(
            {
                "query_id": query_id,
                "query": query_text,
                "category": category,
                "difficulty": difficulty,
                "expected_ids": expected_ids,
                "retrieved_ids": [d.get("id", "") for d in retrieved],
                "retrieved_count": len(retrieved),
            }
        )
        results.append(metrics)

        # 进度显示
        if idx % progress_interval == 0 or idx == total:
            pct = idx / total * 100
            sys.stdout.write(
                f"\r  进度: [{('#' * (idx // progress_interval)).ljust(20)}] "
                f"{idx}/{total} ({pct:.0f}%)"
            )
            sys.stdout.flush()
    t_elapsed = time.monotonic() - t_start
    print()
    print(f"  耗时: {t_elapsed:.1f}s | 平均: {t_elapsed / total:.3f}s/query")
    print()

    # 4. 汇总统计
    print("[4/5] 汇总统计")
    print("-" * 76)

    # -- 全局指标 --
    hits = sum(1 for r in results if r["hit"])
    hit_rate = hits / total
    avg_recall = sum(r["recall_at_k"] for r in results) / total
    avg_precision = sum(r["precision_at_k"] for r in results) / total
    avg_mrr = sum(r["mrr"] for r in results) / total
    avg_ndcg = sum(r["ndcg_at_k"] for r in results) / total

    print()
    print(f"  查询总数: {total}")
    print(f"  命中数: {hits}/{total}")
    print()
    print("  ┌──────────────────────────────────────────────┐")
    print("  │            RAG 检索质量核心指标               │")
    print("  ├──────────────────────────────────────────────┤")
    print(f"  │  Hit Rate (Top-{K}):           {hit_rate:>8.1%}            │")
    print(f"  │  Recall@{K}:                    {avg_recall:>8.1%}            │")
    print(f"  │  Precision@{K}:                 {avg_precision:>8.1%}            │")
    print(f"  │  MRR:                          {avg_mrr:>8.4f}            │")
    print(f"  │  NDCG@{K}:                      {avg_ndcg:>8.4f}            │")
    print("  └──────────────────────────────────────────────┘")
    print()

    # -- 按难度分组 --
    print("  a) 按难度分组:")
    print(f"  {'难度':<12s}  {'总数':>5s}  {'命中':>5s}  {'命中率':>7s}  {'Recall@3':>9s}  "
          f"{'Precision@3':>12s}  {'MRR':>8s}")
    print(f"  {'-' * 12}  {'-' * 5}  {'-' * 5}  {'-' * 7}  {'-' * 9}  "
          f"{'-' * 12}  {'-' * 8}")

    diff_group = {"easy": [], "medium": [], "hard": []}
    for r in results:
        diff_group.setdefault(r["difficulty"], []).append(r)

    for diff in ["easy", "medium", "hard"]:
        grp = diff_group.get(diff, [])
        if not grp:
            continue
        n = len(grp)
        h = sum(1 for r in grp if r["hit"])
        hr = h / n
        rec = sum(r["recall_at_k"] for r in grp) / n
        prec = sum(r["precision_at_k"] for r in grp) / n
        mrr_v = sum(r["mrr"] for r in grp) / n
        bar = "█" * int(hr * 10) + "░" * (10 - int(hr * 10))
        print(f"  {diff:<12s}  {n:>5d}  {h:>5d}  {hr:>6.1%}  "
              f"{rec:>8.1%}  {prec:>11.1%}  {mrr_v:>7.4f}  {bar}")

    print()

    # -- 按分类分组 --
    print("  b) 按分类分组:")
    print(f"  {'分类':<14s}  {'总数':>4s}  {'命中':>4s}  {'命中率':>7s}  {'Recall@3':>9s}  "
          f"{'Precision@3':>12s}  {'MRR':>8s}")
    print(f"  {'-' * 14}  {'-' * 4}  {'-' * 4}  {'-' * 7}  {'-' * 9}  "
          f"{'-' * 12}  {'-' * 8}")

    cat_group: dict[str, list] = {}
    for r in results:
        cat_group.setdefault(r["category"], []).append(r)

    for cat in ["成分知识", "产品推荐", "使用指导", "售后问题", "投诉处理"]:
        grp = cat_group.get(cat, [])
        if not grp:
            continue
        n = len(grp)
        h = sum(1 for r in grp if r["hit"])
        hr = h / n
        rec = sum(r["recall_at_k"] for r in grp) / n
        prec = sum(r["precision_at_k"] for r in grp) / n
        mrr_v = sum(r["mrr"] for r in grp) / n
        bar = "█" * int(hr * 10) + "░" * (10 - int(hr * 10))
        print(f"  {cat:<14s}  {n:>4d}  {h:>4d}  {hr:>6.1%}  "
              f"{rec:>8.1%}  {prec:>11.1%}  {mrr_v:>7.4f}  {bar}")

    print()

    # 5. 保存 JSON 报告
    print("[5/5] 保存报告...")

    diff_breakdown = {}
    for diff, grp in diff_group.items():
        diff_breakdown[diff] = {
            "total": len(grp),
            "hits": sum(1 for r in grp if r["hit"]),
            "hit_rate": round(sum(1 for r in grp if r["hit"]) / max(len(grp), 1), 4),
            "avg_recall@3": round(sum(r["recall_at_k"] for r in grp) / max(len(grp), 1), 4),
            "avg_precision@3": round(sum(r["precision_at_k"] for r in grp) / max(len(grp), 1), 4),
            "avg_mrr": round(sum(r["mrr"] for r in grp) / max(len(grp), 1), 4),
            "avg_ndcg@3": round(sum(r["ndcg_at_k"] for r in grp) / max(len(grp), 1), 4),
        }

    cat_breakdown = {}
    for cat, grp in cat_group.items():
        cat_breakdown[cat] = {
            "total": len(grp),
            "hits": sum(1 for r in grp if r["hit"]),
            "hit_rate": round(sum(1 for r in grp if r["hit"]) / max(len(grp), 1), 4),
            "avg_recall@3": round(sum(r["recall_at_k"] for r in grp) / max(len(grp), 1), 4),
            "avg_precision@3": round(sum(r["precision_at_k"] for r in grp) / max(len(grp), 1), 4),
            "avg_mrr": round(sum(r["mrr"] for r in grp) / max(len(grp), 1), 4),
            "avg_ndcg@3": round(sum(r["ndcg_at_k"] for r in grp) / max(len(grp), 1), 4),
        }

    report = {
        "metadata": {
            "version": meta["version"],
            "total_queries": total,
            "evaluated_at": str(date.today()),
            "duration_seconds": round(t_elapsed, 2),
            "knowledge_base": kb_name,
        },
        "global": {
            "hit_count": hits,
            "hit_rate": round(hit_rate, 4),
            "avg_recall@3": round(avg_recall, 4),
            "avg_precision@3": round(avg_precision, 4),
            "avg_mrr": round(avg_mrr, 4),
            "avg_ndcg@3": round(avg_ndcg, 4),
            "k": K,
        },
        "difficulty_breakdown": diff_breakdown,
        "category_breakdown": cat_breakdown,
        "per_query_results": [
            {k: r[k] for k in r if k != "retrieved_ids"} for r in results
        ],
    }

    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)
    today_str = date.today().isoformat()
    report_path = reports_dir / f"rag_eval_{today_str}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  JSON 报告已保存: {report_path}")
    print()
    print("=" * 76)

    return report


if __name__ == "__main__":
    asyncio.run(evaluate_rag())
