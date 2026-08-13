#!/usr/bin/env python3
"""
RAG Prefetch Effectiveness Benchmark (Task 3.7)

Compares search latency:
- Without prefetch: sequential classify -> search
- With prefetch: parallel classify + search

Measures latency reduction percentage from overlapping operations.

Usage:
    python3 scripts/benchmark_prefetch.py

Output:
    reports/benchmark_prefetch.json
"""

import asyncio
import json
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Benchmark queries: load from eval set ──
from scripts._benchmark_utils import load_eval_queries

BENCHMARK_QUERIES = load_eval_queries()

# Simulated latency functions (in milliseconds)
COLLECTIONS = ["product_knowledge", "faq", "tech_support"]


async def _simulate_search(query: str, latency_ms: float = 150.0) -> list[dict]:
    """Simulate a RAG knowledge base search with configurable latency."""
    import random as _random

    _random.seed(hash(query) % (2**31))
    # Add jitter
    actual_latency = latency_ms * (0.8 + _random.random() * 0.4)
    await asyncio.sleep(actual_latency / 1000.0)
    return [{"content": f"模拟检索结果：{query}", "score": _random.random(), "collection": c} for c in COLLECTIONS]


async def _simulate_classify(query: str, latency_ms: float = 100.0) -> str:
    """Simulate router classification with configurable latency."""
    import random as _random

    _random.seed(hash(query) % (2**31) + 1)
    actual_latency = latency_ms * (0.8 + _random.random() * 0.4)
    await asyncio.sleep(actual_latency / 1000.0)
    categories = ["product", "faq", "tech", "general"]
    return _random.choice(categories)


async def benchmark_prefetch():
    """Compare search without prefetch vs search with prefetch."""
    print("=" * 70)
    print("  RAG Prefetch Effectiveness Benchmark (Task 3.7)")
    print("=" * 70)
    print()

    # ── Try real knowledge base ──
    _kb = None
    _kb_available = False
    try:
        from core.config import QDRANT_HOST, QDRANT_PORT
        from rag.qdrant_knowledge_base import QdrantKnowledgeBase

        _kb = QdrantKnowledgeBase(host=QDRANT_HOST, port=QDRANT_PORT)
        # Verify connection works by listing collections
        _kb._client.get_collections()
        _kb_available = True
        print(f"[INFO] QdrantKnowledgeBase available (host={QDRANT_HOST})")
    except Exception as e:
        _kb = None
        _kb_available = False
        print(f"[INFO] Knowledge base not available: {e}")
        print("[INFO] Using simulated search for benchmark")
    print()

    # Config (used for simulation only)
    class Config:
        search_latency_ms = 150  # Baseline search latency
        classify_latency_ms = 100  # Baseline classification latency

    queries = BENCHMARK_QUERIES
    prefetch_latencies: list[float] = []
    sequential_latencies: list[float] = []

    # ── Sequential (no prefetch) ──
    print(f"[1/3] Running {len(queries)} queries WITHOUT prefetch (sequential)...")
    for i, query in enumerate(queries):
        start = time.time()

        if _kb_available and _kb is not None:
            # Real: classify first (via router), then search
            from router.query_router import QueryRouter
            try:
                router = QueryRouter()
                await router.route(query)  # classify
            except Exception:
                await asyncio.sleep(Config.classify_latency_ms / 1000.0)
            await _kb.search(query, top_k=3)  # search after classify
        else:
            # Simulated fallback
            _category = await _simulate_classify(query, Config.classify_latency_ms)
            _results = await _simulate_search(query, Config.search_latency_ms)

        elapsed = (time.time() - start) * 1000
        sequential_latencies.append(elapsed)

        if (i + 1) % 10 == 0:
            print(f"    {i + 1}/{len(queries)} done (sequential: {elapsed:.0f}ms)")

    seq_avg = sum(sequential_latencies) / len(sequential_latencies) if sequential_latencies else 0
    print(f"  Sequential avg: {seq_avg:.0f}ms")
    print()

    # ── With prefetch (parallel) ──
    print(f"[2/3] Running {len(queries)} queries WITH prefetch (parallel)...")
    for i, query in enumerate(queries):
        start = time.time()

        if _kb_available and _kb is not None:
            # Real: launch classify and search concurrently (prefetch)
            from router.query_router import QueryRouter

            async def _real_classify(q):
                try:
                    router = QueryRouter()
                    return await router.route(q)
                except Exception:
                    await asyncio.sleep(Config.classify_latency_ms / 1000.0)
                    return None

            classify_task = asyncio.create_task(_real_classify(query))
            search_task = asyncio.create_task(_kb.search(query, top_k=3))
            await asyncio.gather(classify_task, search_task)
        else:
            # Simulated fallback
            classify_task = asyncio.create_task(
                _simulate_classify(query, Config.classify_latency_ms)
            )
            search_task = asyncio.create_task(
                _simulate_search(query, Config.search_latency_ms)
            )
            await asyncio.gather(classify_task, search_task)

        elapsed = (time.time() - start) * 1000
        prefetch_latencies.append(elapsed)

        if (i + 1) % 10 == 0:
            print(f"    {i + 1}/{len(queries)} done (prefetch: {elapsed:.0f}ms)")

    pf_avg = sum(prefetch_latencies) / len(prefetch_latencies) if prefetch_latencies else 0
    print(f"  Prefetch avg: {pf_avg:.0f}ms")
    print()

    # ── Comparison ──
    print("[3/3] Comparison results:")
    print()

    # Calculate reduction
    avg_reduction = ((seq_avg - pf_avg) / seq_avg * 100) if seq_avg > 0 else 0

    # Theoretical optimum: max(classify, search) vs (classify + search)
    theoretical_seq = Config.classify_latency_ms + Config.search_latency_ms
    theoretical_pf = max(Config.classify_latency_ms, Config.search_latency_ms)
    theoretical_reduction = ((theoretical_seq - theoretical_pf) / theoretical_seq * 100)

    print()
    print("  ┌────────────────────────────────────────────────────────────┐")
    print("  │              Prefetch Effectiveness Results                │")
    print("  ├────────────────────────────────┬───────────────────────────┤")
    print("  │ Metric                         │ Value                     │")
    print("  ├────────────────────────────────┼───────────────────────────┤")
    print(f"  │ Sequential avg latency          │ {seq_avg:>7.0f} ms                   │")
    print(f"  │ Prefetch avg latency            │ {pf_avg:>7.0f} ms                   │")
    print(f"  │ Measured latency reduction      │ {avg_reduction:>6.1f}%                     │")
    print(f"  │ Theoretical max reduction       │ {theoretical_reduction:>6.1f}%                     │")
    print(f"  │ Overlap efficiency              │ {avg_reduction / max(theoretical_reduction, 1) * 100:>5.1f}%                     │")
    print("  ├────────────────────────────────┼───────────────────────────┤")
    print(f"  │ Search latency (baseline)       │ {Config.search_latency_ms:>7.0f} ms                   │")
    print(f"  │ Classify latency (baseline)     │ {Config.classify_latency_ms:>7.0f} ms                   │")
    print(f"  │ Parallel speedup factor         │ {seq_avg / max(pf_avg, 0.1):>5.2f}x                     │")
    print("  └────────────────────────────────┴───────────────────────────┘")
    print()

    # ── Save report ──
    print("[4/4] Saving report...")
    report = {
        "benchmark": "rag_prefetch_effectiveness",
        "task": "3.7",
        "config": {
            "queries_count": len(queries),
            "search_latency_ms": Config.search_latency_ms,
            "classify_latency_ms": Config.classify_latency_ms,
            "knowledge_base_available": _kb_available,
            "mode": "REAL" if _kb_available else "SIMULATED",
        },
        "results": {
            "sequential": {
                "avg_latency_ms": round(seq_avg, 2),
                "latencies_ms": [round(v, 2) for v in sequential_latencies],
            },
            "prefetch": {
                "avg_latency_ms": round(pf_avg, 2),
                "latencies_ms": [round(v, 2) for v in prefetch_latencies],
            },
        },
        "comparison": {
            "latency_reduction_pct": round(avg_reduction, 2),
            "theoretical_max_reduction_pct": round(theoretical_reduction, 2),
            "overlap_efficiency_pct": round(
                avg_reduction / max(theoretical_reduction, 1) * 100, 2
            ),
            "speedup_factor": round(seq_avg / max(pf_avg, 0.1), 4),
        },
    }

    report_path = Path(__file__).parent.parent / "reports" / "benchmark_prefetch.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  Report saved: {report_path}")
    print()
    print("=" * 70)
    print("  Benchmark complete!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(benchmark_prefetch())
