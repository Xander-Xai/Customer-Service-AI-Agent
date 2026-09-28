#!/usr/bin/env python3
"""Repeatable local benchmark for Tool Result Context Budget Management.

This benchmark intentionally does not call an external LLM. Token counts use
the repository's tiktoken-or-fallback estimator and latency measures only local
message shaping. API latency is therefore reported as NOT_MEASURED.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.session.token_counter import _count_tokens
from core.tool_result_optimizer import ToolResultOptimizer, compact_old_tool_messages


def _payload(tool: str, count: int, chinese: bool = False) -> Any:
    if tool == "query_customer":
        return {"customer_id": "C001", "name": "王女士", "level": "gold", "address": "上海"}
    rows = []
    for i in range(count):
        if tool == "query_order":
            rows.append({
                "order_id": f"ORD{i:04d}", "status": "已发货", "tracking": f"SF{i:08d}",
                "total": 100 + i, "created": "2026-09-28", "debug_info": "internal" * 8,
            })
        else:
            rows.append({
                "product_id": f"P{i:04d}", "product_name": "烟酰胺精华" if chinese else "serum",
                "stock": 100 + i, "warehouse": "上海仓", "updated": "2026-09-28",
                "raw_payload": "internal" * 10,
            })
    return rows


SCENARIOS = {
    "single_tool_call": [("query_order", 1, False)],
    "three_rounds": [("query_order", 8, False), ("query_inventory", 8, False), ("query_customer", 1, False)],
    "five_rounds": [("query_product", 15, False), ("query_inventory", 15, False), ("query_order", 15, False), ("query_customer", 1, False), ("query_order", 15, False)],
    "large_list": [("query_product", 100, False)],
    "large_json": [("query_order", 50, False)],
    "chinese_text": [("query_product", 30, True)],
    "multi_agent_react": [("query_product", 12, True), ("query_inventory", 12, False), ("query_order", 12, True)],
}


def _run(scenario: list[tuple[str, int, bool]], enabled: bool) -> dict[str, Any]:
    optimizer = ToolResultOptimizer(enabled=enabled)
    messages: list[Any] = []
    raw_tokens = 0
    optimized_tokens = 0
    tool_calls = 0
    start = time.perf_counter()
    for index, (tool_name, item_count, chinese) in enumerate(scenario):
        result = _payload(tool_name, item_count, chinese)
        raw_content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        raw_tokens += _count_tokens(raw_content)
        policy = optimizer.policy_for(tool_name)
        optimized = optimizer.optimize(tool_name, result, policy)
        optimized_tokens += optimized.optimized_token_estimate
        messages.append(AIMessage(content="", tool_calls=[{"id": f"call-{index}", "name": tool_name, "args": {}}]))
        messages.append(ToolMessage(content=optimized.content, tool_call_id=f"call-{index}"))
        if enabled:
            messages[:] = compact_old_tool_messages(messages, preserve_recent=2)
        tool_calls += 1
    elapsed_ms = (time.perf_counter() - start) * 1000
    context_tokens = sum(_count_tokens(getattr(message, "content", "")) for message in messages)
    answer_facts = all(
        any(field in getattr(message, "content", "") for field in ("order_id", "product_id", "customer_id", "status"))
        for message in messages
        if getattr(message, "type", None) == "tool"
    )
    return {
        "tool_result_raw_tokens": raw_tokens,
        "tool_result_optimized_tokens": optimized_tokens,
        "estimated_input_tokens": context_tokens,
        "compression_ratio": round(optimized_tokens / raw_tokens, 4) if raw_tokens else 1.0,
        "tool_call_count": tool_calls,
        "local_optimization_latency_ms": round(elapsed_ms, 3),
        "api_latency": "NOT_MEASURED",
        "task_outcome": "PASS" if answer_facts else "FAIL",
    }


def run_benchmark() -> dict[str, Any]:
    report = {
        "benchmark": "tool_result_context_engineering",
        "token_counter": "core.session.token_counter._count_tokens (tiktoken when installed; fallback is estimated)",
        "api_latency": "NOT_MEASURED",
        "scenarios": {},
    }
    for name, scenario in SCENARIOS.items():
        baseline = _run(scenario, enabled=False)
        optimized = _run(scenario, enabled=True)
        baseline_tokens = baseline["estimated_input_tokens"]
        optimized_tokens = optimized["estimated_input_tokens"]
        report["scenarios"][name] = {
            "baseline": baseline,
            "optimized": optimized,
            "estimated_input_token_reduction_pct": round(
                (baseline_tokens - optimized_tokens) / baseline_tokens * 100, 2
            ) if baseline_tokens else 0.0,
        }
    return report


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Tool Result Context Benchmark",
        "",
        "Local deterministic benchmark; external LLM/API latency is `NOT_MEASURED`.",
        "",
        "| Scenario | Baseline input est. | Optimized input est. | Reduction | Baseline outcome | Optimized outcome |",
        "|---|---:|---:|---:|---|---|",
    ]
    for name, data in report["scenarios"].items():
        base, opt = data["baseline"], data["optimized"]
        lines.append(
            f"| {name} | {base['estimated_input_tokens']} | {opt['estimated_input_tokens']} | "
            f"{data['estimated_input_token_reduction_pct']}% | {base['task_outcome']} | {opt['task_outcome']} |"
        )
    lines += ["", "Latency: local optimizer processing only; API latency: `NOT_MEASURED`."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    report = run_benchmark()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
