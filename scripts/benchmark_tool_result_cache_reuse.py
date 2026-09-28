#!/usr/bin/env python3
"""Deterministic benchmark of avoided tool executions, not ERP/API latency."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The benchmark is deterministic and local; make it runnable without a project
# .env file or an API credential. Existing caller-provided values still win.
os.environ.setdefault("API_KEY_ENABLED", "false")
os.environ.setdefault("DEV_MODE", "true")

from core.tool_result_cache import InMemoryToolResultCache, ToolCachePolicy


async def run_sequence(
    arguments: list[dict[str, Any]], *, enabled: bool, scope: dict[str, str], cache=None
) -> dict[str, Any]:
    cache = cache or InMemoryToolResultCache()
    policy = ToolCachePolicy(enabled=True, ttl_seconds=300)
    calls = 0
    hits = 0
    misses = 0
    lookup_ms = 0.0
    for args in arguments:
        cached = None
        if enabled:
            started = time.perf_counter()
            cached = await cache.get("query_product", args, scope=scope)
            lookup_ms += (time.perf_counter() - started) * 1000
            if cached is None:
                misses += 1
            else:
                hits += 1
        if cached is None:
            calls += 1
            result = [{"product_id": args["keyword"], "name": "deterministic result"}]
            if enabled and policy.enabled:
                await cache.set(
                    "query_product", args, result, scope=scope, ttl_seconds=policy.ttl_seconds
                )
    total = len(arguments)
    return {
        "calls_total": total,
        "cache_hits": hits,
        "cache_misses": misses,
        "real_tool_executions": calls,
        "hit_rate": round(hits / total, 4) if total else 0.0,
        "local_cache_lookup_latency_ms": round(lookup_ms, 4),
        "scope": "same_authenticated_scope",
    }


def main() -> None:
    same = [{"keyword": "A"}] * 100
    mixed = [{"keyword": value} for value in "AABBC"]
    different_scope = [{"keyword": "A"}] * 2
    report = {
        "benchmark": "tool_result_cache_reuse",
        "external_api_latency": "NOT_MEASURED",
        "production_erp_call_reduction": "NOT_MEASURED",
        "stampede_protection": "FUTURE_WORK",
        "scenarios": {
            "100_same_query_disabled": asyncio.run(
                run_sequence(same, enabled=False, scope={"user_id": "u1"})
            ),
            "100_same_query_enabled": asyncio.run(
                run_sequence(same, enabled=True, scope={"user_id": "u1"})
            ),
            "mixed_AABBC_enabled": asyncio.run(
                run_sequence(mixed, enabled=True, scope={"user_id": "u1"})
            ),
            "same_args_different_scope": {},
        },
    }
    shared_cache = InMemoryToolResultCache()
    report["scenarios"]["same_args_different_scope"] = {
        "user_a": asyncio.run(
            run_sequence(different_scope, enabled=True, scope={"user_id": "u1"}, cache=shared_cache)
        ),
        "user_b": asyncio.run(
            run_sequence(different_scope, enabled=True, scope={"user_id": "u2"}, cache=shared_cache)
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
