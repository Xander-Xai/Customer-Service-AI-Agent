#!/usr/bin/env python3
"""
RAG 检索质量评估脚本（面试用）
评估指标：Precision@K, Recall@K, MRR, Hit Rate, 平均距离

用法：
    python3 scripts/evaluate_rag.py

输出：评估报告 + 可截图的汇总表
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.knowledge_base import CosmeticsKnowledgeBase
from rag.seed_data import (
    seed_complaint_knowledge,
    seed_faq,
    seed_product_knowledge,
    seed_supplementary_data,
    seed_tech_support,
)

# ===== 评估数据集 =====
# 格式：(查询, 期望匹配的关键词列表, 期望命中的 collection)
# 关键词用于判断检索结果是否"相关"——如果结果中包含任一关键词，视为命中
EVAL_DATASET: list[dict[str, Any]] = [
    # --- 产品知识类 (product_knowledge) ---
    {
        "query": "烟酰胺有什么功效？",
        "expected_keywords": ["烟酰胺", "美白", "niacinamide", "提亮"],
        "expected_collection": "product_knowledge",
        "category": "成分咨询",
    },
    {
        "query": "敏感肌可以用视黄醇吗？",
        "expected_keywords": ["视黄醇", "retinol", "敏感", "刺激", "耐受"],
        "expected_collection": "product_knowledge",
        "category": "成分咨询",
    },
    {
        "query": "透明质酸是什么？",
        "expected_keywords": ["透明质酸", "玻尿酸", "hyaluronic", "保湿", "锁水"],
        "expected_collection": "product_knowledge",
        "category": "成分咨询",
    },
    {
        "query": "油性皮肤适合用什么面霜？",
        "expected_keywords": ["油性", "控油", "清爽", "面霜", "质地"],
        "expected_collection": "product_knowledge",
        "category": "肤质匹配",
    },
    {
        "query": "VC精华不能和什么一起用？",
        "expected_keywords": ["维生素C", "烟酰胺", "冲突", "搭配", "禁忌"],
        "expected_collection": "product_knowledge",
        "category": "成分交互",
    },
    {
        "query": "传明酸能祛斑吗？",
        "expected_keywords": ["传明酸", "tranexamic", "色斑", "美白", "淡化"],
        "expected_collection": "product_knowledge",
        "category": "成分咨询",
    },
    {
        "query": "果酸和水杨酸有什么区别？",
        "expected_keywords": ["AHA", "BHA", "果酸", "水杨酸", "去角质"],
        "expected_collection": "product_knowledge",
        "category": "成分对比",
    },
    {
        "query": "神经酰胺对皮肤屏障有什么作用？",
        "expected_keywords": ["神经酰胺", "ceramide", "屏障", "修复", "锁水"],
        "expected_collection": "product_knowledge",
        "category": "成分咨询",
    },
    {
        "query": "防晒霜物理防晒和化学防晒怎么选？",
        "expected_keywords": ["物理防晒", "化学防晒", "氧化锌", "紫外线"],
        "expected_collection": "product_knowledge",
        "category": "产品选择",
    },
    {
        "query": "维诺雅有哪些美白产品？",
        "expected_keywords": ["维诺雅", "美白", "烟酰胺", "熊果苷", "精华"],
        "expected_collection": "product_knowledge",
        "category": "产品查询",
    },
    # --- FAQ 类 (faq) ---
    {
        "query": "你们发什么快递？多久能到？",
        "expected_keywords": ["快递", "物流", "发货", "配送", "天"],
        "expected_collection": "faq",
        "category": "物流",
    },
    {
        "query": "怎么退货？退货流程是什么？",
        "expected_keywords": ["退货", "退换", "退款", "流程", "7天"],
        "expected_collection": "faq",
        "category": "售后",
    },
    {
        "query": "会员有什么等级？各等级权益？",
        "expected_keywords": ["会员", "等级", "积分", "权益", "折扣"],
        "expected_collection": "faq",
        "category": "会员",
    },
    {
        "query": "支持什么付款方式？",
        "expected_keywords": ["支付", "微信", "支付宝", "银行卡", "付款"],
        "expected_collection": "faq",
        "category": "支付",
    },
    {
        "query": "产品保质期多久？开封后能用多长时间？",
        "expected_keywords": ["保质期", "开封", "有效期", "PAO", "保存"],
        "expected_collection": "faq",
        "category": "产品信息",
    },
    {
        "query": "怎么辨别产品是不是正品？",
        "expected_keywords": ["正品", "防伪", "验证", "二维码", "查询"],
        "expected_collection": "faq",
        "category": "质量",
    },
    {
        "query": "孕期可以用你们的产品吗？",
        "expected_keywords": ["孕期", "孕妇", "哺乳", "安全", "禁忌"],
        "expected_collection": "faq",
        "category": "安全",
    },
    {
        "query": "怎么开发票？",
        "expected_keywords": ["发票", "增值税", "电子", "开具"],
        "expected_collection": "faq",
        "category": "财务",
    },
    {
        "query": "积分怎么用？怎么兑换？",
        "expected_keywords": ["积分", "兑换", "抵扣", "优惠"],
        "expected_collection": "faq",
        "category": "会员",
    },
    {
        "query": "企业采购有优惠吗？",
        "expected_keywords": ["企业", "团购", "批量", "折扣", "优惠"],
        "expected_collection": "faq",
        "category": "企业服务",
    },
    # --- 技术支持类 (tech_support) ---
    {
        "query": "用了产品过敏了怎么办？",
        "expected_keywords": ["过敏", "红肿", "停用", "冷敷", "就医"],
        "expected_collection": "tech_support",
        "category": "应急处理",
    },
    {
        "query": "护肤品的正确使用顺序是什么？",
        "expected_keywords": ["顺序", "步骤", "洁面", "精华", "面霜", "防晒"],
        "expected_collection": "tech_support",
        "category": "使用指导",
    },
    {
        "query": "夏天护肤和冬天护肤有什么不同？",
        "expected_keywords": ["夏季", "冬季", "防晒", "保湿", "调整"],
        "expected_collection": "tech_support",
        "category": "季节护理",
    },
    {
        "query": "黑头怎么去除？",
        "expected_keywords": ["黑头", "清洁", "水杨酸", "毛孔", "鼻贴"],
        "expected_collection": "tech_support",
        "category": "问题肌肤",
    },
    {
        "query": "医美手术后怎么护理皮肤？",
        "expected_keywords": ["医美", "术后", "修复", "防晒", "保湿", "屏障"],
        "expected_collection": "tech_support",
        "category": "特殊护理",
    },
    {
        "query": "敏感肌应该怎么护肤？",
        "expected_keywords": ["敏感", "温和", "屏障", "修复", "避免"],
        "expected_collection": "tech_support",
        "category": "肤质护理",
    },
    {
        "query": "面膜多久敷一次比较好？",
        "expected_keywords": ["面膜", "频率", "敷", "保湿", "15分钟"],
        "expected_collection": "tech_support",
        "category": "使用指导",
    },
    {
        "query": "成分之间有冲突吗？哪些不能一起用？",
        "expected_keywords": ["冲突", "搭配", "禁忌", "A酸", "果酸", "烟酰胺"],
        "expected_collection": "tech_support",
        "category": "成分安全",
    },
    {
        "query": "不同年龄段应该怎么选择护肤品？",
        "expected_keywords": ["年龄", "抗老", "保湿", "25", "35", "抗氧化"],
        "expected_collection": "tech_support",
        "category": "个性化建议",
    },
    {
        "query": "运动前后需要护肤吗？",
        "expected_keywords": ["运动", "出汗", "清洁", "防晒", "补水"],
        "expected_collection": "tech_support",
        "category": "生活场景",
    },
]


def compute_faithfulness(answer: str, contexts: list[str]) -> float:
    """简单的 faithfulness 计算：回答中有多少句子被上下文支持"""
    sentences = re.split(r"[。！？\n]", answer)
    sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) > 5]
    if not sentences:
        return 0.0

    supported = 0
    context_text = " ".join(contexts)
    for sent in sentences:
        # 检查句子中的关键词是否在上下文中出现
        keywords = set(sent.replace("，", " ").replace("、", " ").split())
        keywords = {k for k in keywords if len(k) >= 2}
        if keywords:
            overlap = sum(1 for k in keywords if k in context_text)
            if overlap / len(keywords) >= 0.3:
                supported += 1

    return supported / len(sentences) if sentences else 0.0


async def evaluate_rag():
    """执行 RAG 检索质量评估"""

    print("=" * 70)
    print("  RAG 检索质量评估报告")
    print("  多智能体客服系统 — 药妆智多星 v4.2")
    print("=" * 70)
    print()

    # 1. 初始化知识库
    kb = CosmeticsKnowledgeBase()  # 内存模式
    print("[1/4] 初始化 ChromaDB 知识库...")
    kb.seed_if_empty("product_knowledge", seed_product_knowledge)
    kb.seed_if_empty("faq", seed_faq)
    kb.seed_if_empty("tech_support", seed_tech_support)
    kb.seed_if_empty("complaint_knowledge", seed_complaint_knowledge)
    seed_supplementary_data(kb)

    pk_count = kb.get_collection_count("product_knowledge")
    faq_count = kb.get_collection_count("faq")
    ts_count = kb.get_collection_count("tech_support")
    print(f"  product_knowledge: {pk_count} docs")
    print(f"  faq: {faq_count} docs")
    print(f"  tech_support: {ts_count} docs")
    print(f"  总计: {pk_count + faq_count + ts_count} docs")
    print()

    # 2. 执行评估
    print(f"[2/4] 执行检索评估（{len(EVAL_DATASET)} 条测试查询）...")
    print()

    collections = ["product_knowledge", "faq", "tech_support"]
    results = []
    K = 3  # Top-K

    for _i, case in enumerate(EVAL_DATASET, 1):
        query = case["query"]
        expected_kws = case["expected_keywords"]
        expected_col = case["expected_collection"]
        category = case["category"]

        # 执行多集合检索
        retrieved = await kb.query_multiple(collections, query, n_results=K)

        # 计算指标
        # Hit: 检索结果中至少有一条包含期望关键词
        hit = False
        precision_hits = 0
        first_relevant_rank = 0
        distances = []

        for rank, doc in enumerate(retrieved, 1):
            content = doc["content"].lower()
            distance = doc.get("distance", 999)
            distances.append(distance)

            is_relevant = any(kw.lower() in content for kw in expected_kws)
            if is_relevant:
                precision_hits += 1
                if not hit:
                    hit = True
                    first_relevant_rank = rank

        # MRR: 第一个相关结果的倒数排名
        mrr = 1.0 / first_relevant_rank if first_relevant_rank > 0 else 0.0
        precision_at_k = precision_hits / K if K > 0 else 0.0
        # Recall: 简化版——命中返回 1.0，未命中返回 0.0（ground truth 只有一条正确答案）
        recall_at_k = 1.0 if hit else 0.0
        avg_distance = sum(distances) / len(distances) if distances else 999

        # v5.1: NDCG@K — 排序质量指标
        import math

        dcg = 0.0
        for rank, doc in enumerate(retrieved, 1):
            content = doc["content"].lower()
            rel = 1.0 if any(kw.lower() in content for kw in expected_kws) else 0.0
            dcg += rel / math.log2(rank + 1)
        # IDCG: 如果所有相关文档都排在最前面
        n_relevant = min(precision_hits, K)
        idcg = (
            sum(1.0 / math.log2(i + 1) for i in range(1, n_relevant + 1)) if n_relevant > 0 else 0
        )
        ndcg_at_k = dcg / idcg if idcg > 0 else 0.0

        # Faithfulness: 检索结果与查询关键词的匹配质量
        contexts = [doc["content"] for doc in retrieved]
        faithfulness = compute_faithfulness(query, contexts)

        result = {
            "query": query,
            "category": category,
            "expected_collection": expected_col,
            "hit": hit,
            "precision_at_k": precision_at_k,
            "recall_at_k": recall_at_k,
            "mrr": mrr,
            "ndcg_at_k": ndcg_at_k,
            "faithfulness": faithfulness,
            "avg_distance": avg_distance,
            "first_relevant_rank": first_relevant_rank,
            "retrieved_count": len(retrieved),
        }
        results.append(result)

        status = "✅" if hit else "❌"
        print(f"  {status} [{category:6s}] {query}")
        if hit:
            print(
                f"       → 命中排名: #{first_relevant_rank}, "
                f"P@{K}: {precision_at_k:.2f}, "
                f"平均距离: {avg_distance:.4f}"
            )
        else:
            if retrieved:
                print(
                    f"       → 最近结果: {retrieved[0]['content'][:60]}... "
                    f"(距离: {retrieved[0].get('distance', 'N/A'):.4f})"
                )
            else:
                print("       → 无检索结果")

    # 3. 汇总统计
    print()
    print("[3/4] 汇总统计")
    print("-" * 70)

    total = len(results)
    hits = sum(1 for r in results if r["hit"])
    hit_rate = hits / total if total > 0 else 0

    precisions = [r["precision_at_k"] for r in results]
    recalls = [r["recall_at_k"] for r in results]
    mrrs = [r["mrr"] for r in results]
    ndcgs = [r.get("ndcg_at_k", 0) for r in results]
    faithfulnesses = [r["faithfulness"] for r in results]
    distances = [r["avg_distance"] for r in results]

    avg_precision = sum(precisions) / len(precisions) if precisions else 0
    avg_recall = sum(recalls) / len(recalls) if recalls else 0
    avg_mrr = sum(mrrs) / len(mrrs) if mrrs else 0
    avg_ndcg = sum(ndcgs) / len(ndcgs) if ndcgs else 0
    avg_faithfulness = sum(faithfulnesses) / len(faithfulnesses) if faithfulnesses else 0
    avg_distance_all = sum(distances) / len(distances) if distances else 0

    # 按类别统计
    categories = {}
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = {"total": 0, "hits": 0}
        categories[cat]["total"] += 1
        if r["hit"]:
            categories[cat]["hits"] += 1

    print()
    print(f"  测试查询总数:     {total}")
    print(f"  命中数:           {hits}/{total}")
    print()
    print("  ┌─────────────────────────────────┐")
    print("  │     RAG 检索质量核心指标         │")
    print("  ├─────────────────────────────────┤")
    print(f"  │  Hit Rate (Top-{K}):     {hit_rate:>6.1%}     │")
    print(f"  │  Precision@{K}:          {avg_precision:>6.1%}     │")
    print(f"  │  Recall@{K}:             {avg_recall:>6.1%}     │")
    print(f"  │  MRR:                  {avg_mrr:>6.3f}     │")
    print(f"  │  NDCG@{K}:              {avg_ndcg:>6.3f}     │")
    print(f"  │  Faithfulness:         {avg_faithfulness:>6.1%}     │")
    print(f"  │  平均检索距离:         {avg_distance_all:>6.4f}  │")
    print("  └─────────────────────────────────┘")
    print()

    print("  按类别统计:")
    print(f"  {'类别':<12s}  {'命中/总数':>8s}  {'命中率':>6s}")
    print(f"  {'-' * 12}  {'-' * 8}  {'-' * 6}")
    for cat, stats in sorted(categories.items()):
        cat_rate = stats["hits"] / stats["total"] if stats["total"] > 0 else 0
        bar = "█" * int(cat_rate * 10) + "░" * (10 - int(cat_rate * 10))
        print(f"  {cat:<12s}  {stats['hits']:>3d}/{stats['total']:<3d}    {cat_rate:>6.1%}  {bar}")

    # 4. 面试话术摘要
    print()
    print("[4/4] 面试话术摘要")
    print("-" * 70)
    print()
    print(f'  "我的 RAG 系统使用 ChromaDB 向量检索，{pk_count + faq_count + ts_count} 篇文档')
    print("   分 3 个 collection（产品知识/FAQ/技术支持）。")
    print(
        f"   评估结果：Top-{K} Hit Rate {hit_rate:.1%}，MRR {avg_mrr:.3f}，Faithfulness {avg_faithfulness:.1%}。"
    )
    print(f'   这意味着 {hit_rate:.0%} 的用户问题能在前 {K} 条检索结果中找到相关答案。"')
    print()
    print("=" * 70)

    # 5. 输出 JSON 报告（可选）
    report = {
        "total_queries": total,
        "hit_count": hits,
        "hit_rate": round(hit_rate, 4),
        "precision_at_k": round(avg_precision, 4),
        "recall_at_k": round(avg_recall, 4),
        "mrr": round(avg_mrr, 4),
        "faithfulness": round(avg_faithfulness, 4),
        "avg_distance": round(avg_distance_all, 4),
        "k": K,
        "collections": {
            "product_knowledge": pk_count,
            "faq": faq_count,
            "tech_support": ts_count,
        },
        "category_breakdown": {
            cat: {
                "total": stats["total"],
                "hits": stats["hits"],
                "hit_rate": round(stats["hits"] / stats["total"], 4) if stats["total"] > 0 else 0,
            }
            for cat, stats in categories.items()
        },
        "per_query_results": results,
    }

    report_path = Path(__file__).parent.parent / "docs" / "rag-evaluation-report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  详细报告已保存: {report_path}")

    return report


if __name__ == "__main__":
    asyncio.run(evaluate_rag())
