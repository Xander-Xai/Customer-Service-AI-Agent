#!/usr/bin/env python3
"""
Cache Hit Rate Load Test (Task 3.2)

Tests cache hit rates under load by warming up the cache with a set of queries
and then running repeated queries to measure L1 hit rate, overall hit rate, and QPS.

Usage:
    python3 scripts/benchmark_cache.py

Output:
    reports/benchmark_cache.json
"""

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Benchmark queries (cosmetics domain, same pattern as evaluate_rag.py) ──
BENCHMARK_QUERIES = [
    # Product knowledge
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
    # FAQ
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
    # Tech support
    "用了产品过敏了怎么办？",
    "护肤品的正确使用顺序是什么？",
    "夏天护肤和冬天护肤有什么不同？",
    "黑头怎么去除？",
    "医美手术后怎么护理皮肤？",
    "敏感肌应该怎么护肤？",
    "面膜多久敷一次比较好？",
    "成分之间有冲突吗？哪些不能一起用？",
    "不同年龄段应该怎么选择护肤品？",
    "运动前后需要护肤吗？",
    # Additional general queries
    "请问你们的精华液含有哪些主要成分？适合敏感肌使用吗？",
    "面霜和精华液的正确使用顺序是什么？",
    "玻尿酸面膜多久用一次？",
    "你们有哪些适合油性皮肤的产品？",
    "烟酰胺美白霜的主要功效是什么？",
    "洁面乳的正确使用方法是什么？",
    "面膜敷多长时间最合适？",
    "护肤品使用顺序是怎样的？",
    "退换货政策是什么？",
    "产品过敏可以退货吗？",
    "如何申请退款？",
    "你们的客服工作时间是什么？",
    "如何成为VIP会员？",
    "积分兑换规则是什么？",
    "你们有实体店吗？在哪里？",
    "产品可以试用吗？",
    "过敏体质可以用你们的护肤品吗？",
    "美白产品要多久见效？",
    "抗衰老产品适合什么年龄？",
    "痘痘肌适合用什么产品？",
    # Product-specific comparisons
    "氨基酸洁面和皂基洁面有什么区别？",
    "保湿面霜和乳液的区别？",
    "精华液和精油一样吗？",
    "晚安面膜和普通面膜的区别？",
    "素颜霜和BB霜的区别？",
    # More volume queries
    "水乳霜的正确使用顺序",
    "眼霜的正确涂抹方法",
    "颈纹怎么淡化？",
    "面膜可以每天敷吗？",
    "去角质多久做一次合适？",
    "防晒霜需要卸妆吗？",
    "隔离霜和防晒霜的区别？",
    "皮肤屏障受损怎么修复？",
    "油皮和干皮如何区分？",
    "混合性皮肤怎么护理？",
]


async def benchmark_cache():
    """Cache hit rate load test: warm up + repeated queries."""
    print("=" * 70)
    print("  Cache Hit Rate Load Test (Task 3.2)")
    print("=" * 70)
    print()

    # ── Initialize container ──
    print("[1/5] Initializing service container...")
    try:
        from core.container import ServiceContainer

        container = ServiceContainer()
        await container.initialize()
        cache = container.cache
    except Exception as e:
        print(f"  ERROR: Failed to initialize container: {e}")
        print("  Creating standalone ResponseCache for testing...")
        try:
            from cache.response_cache import ResponseCache

            cache = ResponseCache()
            container = None
        except Exception as e2:
            print(f"  ERROR: Cannot create ResponseCache either: {e2}")
            _save_error_report(f"Container: {e}, ResponseCache: {e2}")
            return

    if cache is None:
        print("  WARNING: Cache is None, creating bare ResponseCache.")
        try:
            from cache.response_cache import ResponseCache

            cache = ResponseCache()
        except Exception as e:
            print(f"  ERROR: {e}")
            _save_error_report(str(e))
            return

    # ── Show cache config ──
    print(f"  Cache type: {type(cache).__name__}")
    l1_avail = "Yes" if cache._redis is not None else "No (in-memory only)"
    l2_avail = "Yes" if cache._qdrant is not None else "No"
    print(f"  L1 (Redis):      {l1_avail}")
    print(f"  L2 (Qdrant):     {l2_avail}")
    print(f"  L3 (Jaccard):    {'Yes' if cache._fallback_enabled else 'No'}")
    print()

    # ── Step 2: Warm up cache ──
    warmup_count = min(200, len(BENCHMARK_QUERIES))
    print(f"[2/5] Warming up cache with first {warmup_count} queries...")

    warmed = 0
    warmup_start = time.time()
    for _i, query in enumerate(BENCHMARK_QUERIES[:warmup_count]):
        try:
            dummy_response = f"这是关于「{query}」的参考回答。"
            cache.set(
                query,
                dummy_response,
                metadata={"intent_type": "knowledge_qa", "user_role": "customer"},
            )
            warmed += 1
            if (_i + 1) % 50 == 0:
                print(f"    Warmed {_i + 1}/{warmup_count}...")
        except Exception as e:
            print(f"    WARNING: Failed to cache query {_i}: {e}")

    warmup_elapsed = time.time() - warmup_start
    print(f"  Cache warmed: {warmed}/{warmup_count} queries in {warmup_elapsed:.2f}s")
    print()

    # ── Step 3: Test loop ──
    repeat = max(1, 1000 // warmup_count)
    total_queries = warmup_count * repeat
    print(f"[3/5] Running {total_queries} queries ({warmup_count} queries x {repeat} iterations)...")

    # Reset stats for clean measurement
    if hasattr(cache, "_stats"):
        cache._stats = {"l1_hits": 0, "l2_hits": 0, "fallback_hits": 0, "misses": 0}

    test_start = time.time()
    for iteration in range(repeat):
        for _i, query in enumerate(BENCHMARK_QUERIES[:warmup_count]):
            cache.get(
                query,
                metadata={"intent_type": "knowledge_qa", "user_role": "customer"},
            )
        if (iteration + 1) % 2 == 0 or iteration == 0 or iteration == repeat - 1:
            print(f"    Iteration {iteration + 1}/{repeat} done...")

    test_elapsed = time.time() - test_start

    # ── Step 4: Collect results ──
    print()
    print("[4/5] Collecting cache statistics...")

    raw_l1 = cache._stats.get("l1_hits", 0)
    raw_l2 = cache._stats.get("l2_hits", 0)
    raw_fb = cache._stats.get("fallback_hits", 0)
    raw_misses = cache._stats.get("misses", 0)
    raw_total = raw_l1 + raw_l2 + raw_fb + raw_misses

    qps = raw_total / test_elapsed if test_elapsed > 0 else 0
    net_hit_rate = ((raw_l1 + raw_l2 + raw_fb) / raw_total * 100) if raw_total > 0 else 0.0

    print()
    print("  ┌──────────────────────────────────────────┐")
    print("  │         Cache Benchmark Results          │")
    print("  ├──────────────────────────────────────────┤")
    print(f"  │  Warmup queries:    {warmed:>6d}                │")
    print(f"  │  Test queries:      {raw_total:>6d}                │")
    print(f"  │  L1 hits:           {raw_l1:>6d}                │")
    print(f"  │  L2 hits:           {raw_l2:>6d}                │")
    print(f"  │  L3 (fallback) hits:{raw_fb:>6d}                │")
    print(f"  │  Misses:            {raw_misses:>6d}                │")
    print(f"  │  Hit rate:          {net_hit_rate:>6.1f}%               │")
    print(f"  │  QPS:               {qps:>8.1f}           │")
    print(f"  │  Test duration:     {test_elapsed:>6.2f}s              │")
    print("  └──────────────────────────────────────────┘")
    print()

    # ── Step 5: Save report ──
    print("[5/5] Saving report...")
    report = {
        "benchmark": "cache_hit_rate",
        "task": "3.2",
        "config": {
            "warmup_queries": warmup_count,
            "repeat_count": repeat,
            "total_queries": total_queries,
            "l1_available": l1_avail,
            "l2_available": l2_avail,
            "l3_enabled": cache._fallback_enabled if hasattr(cache, "_fallback_enabled") else False,
        },
        "results": {
            "warmed": warmed,
            "warmup_duration_s": round(warmup_elapsed, 3),
            "l1_hits": raw_l1,
            "l2_hits": raw_l2,
            "fallback_hits": raw_fb,
            "misses": raw_misses,
            "total": raw_total,
            "hit_rate_pct": round(net_hit_rate, 2),
            "qps": round(qps, 2),
            "test_duration_s": round(test_elapsed, 3),
        },
    }

    report_path = Path(__file__).parent.parent / "reports" / "benchmark_cache.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  Report saved: {report_path}")
    print()
    print("=" * 70)
    print("  Benchmark complete!")
    print("=" * 70)

    # Cleanup
    if container is not None:
        with contextlib.suppress(Exception):
            await container.close()


def _save_error_report(error: str):
    """Save an error report when initialization fails."""
    report = {
        "benchmark": "cache_hit_rate",
        "task": "3.2",
        "error": str(error),
        "status": "failed",
    }
    report_path = Path(__file__).parent.parent / "reports" / "benchmark_cache.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fmt:
        json.dump(report, fmt, ensure_ascii=False, indent=2)
    print(f"  Error report saved: {report_path}")


if __name__ == "__main__":
    asyncio.run(benchmark_cache())
