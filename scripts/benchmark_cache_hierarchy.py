#!/usr/bin/env python3
"""
Three-Level Cache Hierarchy Test (Task 3.3)

Tests all three cache layers:
- L1: MD5 exact match (Redis)
- L2: Semantic variant match (Qdrant vector)
- L3: Jaccard fallback (in-memory)
- Miss: No match at any layer

Expected distribution: L1 ~35%, L2 ~30%, L3 ~5%, Miss ~30%

Usage:
    python3 scripts/benchmark_cache_hierarchy.py

Output:
    reports/benchmark_cache_hierarchy.json
"""

import asyncio
import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Load base queries from eval set ──
from scripts._benchmark_utils import load_eval_queries

_eval_queries = load_eval_queries()
# Use first 25 eval-set queries as exact-match seed (more diverse than hardcoded)
EXACT_QUERIES = _eval_queries[:25] if len(_eval_queries) >= 25 else [
    "烟酰胺有什么功效？",
    "敏感肌可以用视黄醇吗？",
    "透明质酸是什么？",
    "油性皮肤适合用什么面霜？",
    "VC精华不能和什么一起用？",
    "传明酸能祛斑吗？",
    "果酸和水杨酸有什么区别？",
    "神经酰胺对皮肤屏障有什么作用？",
    "防晒霜物理防晒和化学防晒怎么选？",
    "维诺雅有哪些美白产品？",
    "你们发什么快递？多久能到？",
    "怎么退货？退货流程是什么？",
    "会员有什么等级？各等级权益？",
    "支持什么付款方式？",
    "产品保质期多久？开封后能用多长时间？",
    "怎么辨别产品是不是正品？",
    "孕期可以用你们的产品吗？",
    "怎么开发票？",
    "积分怎么用？怎么兑换？",
    "企业采购有优惠吗？",
    "用了产品过敏了怎么办？",
    "护肤品的正确使用顺序是什么？",
    "夏天护肤和冬天护肤有什么不同？",
    "黑头怎么去除？",
    "医美手术后怎么护理皮肤？",
]

SEMANTIC_VARIANTS = [
    ("烟酰胺有什么功效？", "烟酰胺的护肤功效有哪些？"),
    ("敏感肌可以用视黄醇吗？", "敏感皮肤能不能用视黄醇产品？"),
    ("透明质酸是什么？", "玻尿酸是什么成分？"),
    ("油性皮肤适合用什么面霜？", "油皮适合什么样的面霜？"),
    ("VC精华不能和什么一起用？", "维生素C精华不能搭配什么？"),
    ("传明酸能祛斑吗？", "传明酸对色斑有效果吗？"),
    ("果酸和水杨酸有什么区别？", "AHA和BHA有什么不同？"),
    ("神经酰胺对皮肤屏障有什么作用？", "神经酰胺对皮肤屏障功能有帮助吗？"),
    ("防晒霜物理防晒和化学防晒怎么选？", "物理防晒和化学防晒哪个好？"),
    ("你们发什么快递？多久能到？", "你们用哪家快递发货？几天能收到？"),
    ("怎么退货？退货流程是什么？", "退货流程怎么走？"),
    ("会员有什么等级？各等级权益？", "会员等级和权益有哪些？"),
    ("支持什么付款方式？", "可以用哪些方式支付？"),
    ("产品保质期多久？开封后能用多长时间？", "保质期多长时间？开封后多久要用完？"),
    ("怎么辨别产品是不是正品？", "如何验证产品真伪？"),
    ("用了产品过敏了怎么办？", "使用产品后过敏怎么处理？"),
    ("护肤品的正确使用顺序是什么？", "护肤品应该按什么顺序使用？"),
    ("夏天护肤和冬天护肤有什么不同？", "夏季和冬季的护肤步骤有什么差异？"),
    ("黑头怎么去除？", "去黑头有什么好方法？"),
    ("医美手术后怎么护理皮肤？", "做完医美后如何护肤？"),
]

JACCARD_VARIANTS = [
    ("烟酰胺有什么功效？", "烟酰胺有哪些护肤作用？"),
    ("怎么退货", "如何办理退换货"),
    ("会员有什么等级", "会员等级制度"),
    ("支持什么付款方式", "支付方式有哪些"),
    ("产品保质期多久", "产品有效期多久"),
    ("怎么辨别产品是不是正品", "如何查真伪"),
    ("护肤品使用顺序", "护肤步骤顺序"),
    ("黑头怎么去除", "怎么去黑头"),
    ("积分怎么用", "积分如何使用"),
    ("过敏了怎么办", "使用过敏处理"),
]

# Queries that should miss entirely
MISS_QUERIES = [
    "今天天气怎么样？",
    "量子力学是什么？",
    "怎么做红烧肉？",
    "帮我写一首诗",
    "Python和Java哪个好？",
    "明天会下雨吗？",
    "地球到月球有多远？",
    "怎么学英语？",
    "推荐一部好看的电影",
    "什么是人工智能？",
    "李白是哪个朝代的？",
    "比特币现在多少钱？",
    "怎么做蛋糕？",
    "如何提高睡眠质量？",
    "长城有多长？",
    "水在零度会怎样？",
    "光合作用的原理是什么？",
    "怎么练腹肌？",
    "什么是区块链？",
    "如何养猫？",
]


async def benchmark_cache_hierarchy():
    """Test three-level cache hierarchy hit rates."""
    print("=" * 70)
    print("  Three-Level Cache Hierarchy Test (Task 3.3)")
    print("=" * 70)
    print()

    # ── Initialize ──
    print("[1/5] Initializing cache...")
    try:
        from cache.response_cache import ResponseCache

        cache = ResponseCache()
    except Exception as e:
        print(f"  ERROR: {e}")
        _save_error_report(str(e))
        return

    print(f"  Cache initialized: L1 Redis={cache._redis is not None}, "
          f"L2 Qdrant={cache._qdrant is not None}, "
          f"L3 Jaccard={cache._fallback_enabled}")
    print()

    # ── Seed L1 (exact matches) ──
    print("[2/5] Seeding L1 cache with exact queries...")
    for q in EXACT_QUERIES:
        cache.set(q, f"回答：{q}", metadata={"intent_type": "knowledge_qa", "user_role": "customer"})
    print(f"  Seeded {len(EXACT_QUERIES)} L1 entries")
    print()

    # ── Run tests ──
    print("[3/5] Running hierarchy tests...")

    # Reset stats
    cache._stats = {"l1_hits": 0, "l2_hits": 0, "fallback_hits": 0, "misses": 0}

    # L1 test: exact queries (should hit L1)
    print("  Testing L1 (exact matches)...")
    for q in EXACT_QUERIES:
        cache.get(q, metadata={"intent_type": "knowledge_qa", "user_role": "customer"})

    # L2 test: semantic variants (should hit L2 if Qdrant available, else may hit L3 or miss)
    print("  Testing L2 (semantic variants)...")
    for _, variant in SEMANTIC_VARIANTS:
        cache.get(variant, metadata={"intent_type": "knowledge_qa", "user_role": "customer"})

    # L3 test: Jaccard variants (should hit L3 if similarity high enough)
    print("  Testing L3 (Jaccard fallback)...")
    for _, variant in JACCARD_VARIANTS:
        cache.get(variant, metadata={"intent_type": "knowledge_qa", "user_role": "customer"})

    # Miss test: unrelated queries (should miss all layers)
    print("  Testing misses (unrelated queries)...")
    for q in MISS_QUERIES:
        cache.get(q, metadata={"intent_type": "default", "user_role": "customer"})

    # ── Collect results ──
    print()
    print("[4/5] Results:")

    stats = cache._stats
    l1 = stats["l1_hits"]
    l2 = stats["l2_hits"]
    l3 = stats["fallback_hits"]
    misses = stats["misses"]
    total = l1 + l2 + l3 + misses

    l1_rate = (l1 / total * 100) if total > 0 else 0
    l2_rate = (l2 / total * 100) if total > 0 else 0
    l3_rate = (l3 / total * 100) if total > 0 else 0
    miss_rate = (misses / total * 100) if total > 0 else 0
    total_hit_rate = ((l1 + l2 + l3) / total * 100) if total > 0 else 0

    # ── Validate against target ──
    TARGET_HIT_RATE = 65.0  # Minimum acceptable total hit rate (%)
    passed = total_hit_rate >= TARGET_HIT_RATE
    status = "PASS ✅" if passed else "FAIL ❌"

    print()
    print("  ┌───────────────────────────────────────────────┐")
    print("  │          Cache Hierarchy Results              │")
    print("  ├───────────────────────────────────────────────┤")
    print("  │  Layer     │  Hits   │  Rate       │  Status  │")
    print("  ├────────────┼─────────┼─────────────┼──────────┤")
    print(f"  │  L1 (MD5)  │  {l1:>5d}  │  {l1_rate:>5.1f}%      │  {'Yes' if l1 > 0 else 'No'}       │")
    print(f"  │  L2 (Qd)   │  {l2:>5d}  │  {l2_rate:>5.1f}%      │  {'Yes' if l2 > 0 else 'No'}       │")
    print(f"  │  L3 (Jac)  │  {l3:>5d}  │  {l3_rate:>5.1f}%      │  {'Yes' if l3 > 0 else 'No'}       │")
    print(f"  │  Miss      │  {misses:>5d}  │  {miss_rate:>5.1f}%      │          │")
    print("  ├────────────┼─────────┼─────────────┼──────────┤")
    print(f"  │  Total     │  {l1 + l2 + l3:>5d}  │  {total_hit_rate:>5.1f}%      │  {status}  │")
    print("  └───────────────────────────────────────────────┘")
    print()
    print(f"  Target: total hit rate ≥ {TARGET_HIT_RATE:.0f}%  →  {status}")
    print()

    if l1 > 0 and l2 == 0 and l3 > 0:
        print("  NOTE: L2 (Qdrant) detected as unavailable; L3 (Jaccard) activated as fallback.")
    if l1 > 0 and l2 == 0 and l3 == 0:
        print("  NOTE: Only L1 (Redis) available; no fallback layers detected.")
    print()

    # ── Save report ──
    print("[5/5] Saving report...")
    report = {
        "benchmark": "cache_hierarchy",
        "task": "3.3",
        "config": {
            "l1_enabled": cache._redis is not None,
            "l2_enabled": cache._qdrant is not None,
            "l3_enabled": cache._fallback_enabled,
            "exact_queries": len(EXACT_QUERIES),
            "semantic_variants": len(SEMANTIC_VARIANTS),
            "jaccard_variants": len(JACCARD_VARIANTS),
            "miss_queries": len(MISS_QUERIES),
        },
        "results": {
            "l1_hits": l1,
            "l1_rate_pct": round(l1_rate, 2),
            "l2_hits": l2,
            "l2_rate_pct": round(l2_rate, 2),
            "l3_hits": l3,
            "l3_rate_pct": round(l3_rate, 2),
            "misses": misses,
            "miss_rate_pct": round(miss_rate, 2),
            "total": total,
            "total_hit_rate_pct": round(total_hit_rate, 2),
        },
        "validation": {
            "target_hit_rate_pct": TARGET_HIT_RATE,
            "actual_hit_rate_pct": round(total_hit_rate, 2),
            "passed": passed,
        },
        "notes": {
            "l1_performance": "exact_match",
            "l2_performance": "semantic" if l2 > 0 else ("jaccard_fallback" if l3 > 0 else "unavailable"),
            "l3_performance": "jaccard_fallback" if l3 > 0 else "inactive",
        },
    }

    report_path = Path(__file__).parent.parent / "reports" / "benchmark_cache_hierarchy.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  Report saved: {report_path}")
    print()
    print("=" * 70)
    print("  Benchmark complete!")
    print("=" * 70)


def _save_error_report(error: str):
    """Save an error report when initialization fails."""
    report = {
        "benchmark": "cache_hierarchy",
        "task": "3.3",
        "error": str(error),
        "status": "failed",
    }
    report_path = Path(__file__).parent.parent / "reports" / "benchmark_cache_hierarchy.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fmt:
        json.dump(report, fmt, ensure_ascii=False, indent=2)
    print(f"  Error report saved: {report_path}")


if __name__ == "__main__":
    asyncio.run(benchmark_cache_hierarchy())
