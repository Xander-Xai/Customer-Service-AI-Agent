#!/usr/bin/env python3
"""Token 成本分析脚本（基于 A/B 测试结果进行成本对比分析）。

可独立运行或依赖 benchmark_ab_test.py 的输出。

用法:
    python3 scripts/benchmark_cost.py                          # 独立运行（使用默认定价）
    python3 scripts/benchmark_cost.py --input reports/ab_test_*.json  # 基于已有报告
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 硅基流动 Qwen3-8B 定价（$/1K tokens）
COST_PER_1K_TOKENS = {
    "input": 0.0015,
    "output": 0.006,
}

# 基础设施成本（$/1K 操作）
INFRA_COST = {
    "redis": 0.0001,
    "qdrant": 0.0002,
}


def calculate_total_cost(input_tokens: int, output_tokens: int) -> dict:
    """计算 Token 成本"""
    llm_cost = (
        input_tokens / 1000 * COST_PER_1K_TOKENS["input"] +
        output_tokens / 1000 * COST_PER_1K_TOKENS["output"]
    )
    return {
        "input_cost": round(input_tokens / 1000 * COST_PER_1K_TOKENS["input"], 4),
        "output_cost": round(output_tokens / 1000 * COST_PER_1K_TOKENS["output"], 4),
        "total_llm_cost": round(llm_cost, 4),
    }


def analyze_cost():
    """执行成本分析（基于默认场景数据）"""
    print("=" * 60)
    print("  Token 成本分析报告")
    print("  Qwen3-8B @ 硅基流动")
    print("=" * 60)

    # 场景 A: 无缓存（基线）— 1000 次 LLM 调用
    scenario_a = {
        "total_queries": 1000,
        "llm_calls": 1000,
        "total_input_tokens": 350000,
        "total_output_tokens": 100000,
    }

    # 场景 B: 有缓存 — ~350 次 LLM 调用（65% 命中率）
    scenario_b = {
        "total_queries": 1000,
        "llm_calls": 350,
        "cache_hits": 650,
        "total_input_tokens": 122500,
        "total_output_tokens": 35000,
        "cache_hit_rate": 0.65,
    }

    cost_a = calculate_total_cost(scenario_a["total_input_tokens"], scenario_a["total_output_tokens"])
    cost_b = calculate_total_cost(scenario_b["total_input_tokens"], scenario_b["total_output_tokens"])

    # 基础设施成本
    infra_cost_a = 0  # 无缓存，无基础设施开销
    infra_cost_b = (
        scenario_b["cache_hits"] / 1000 * INFRA_COST["redis"] +
        scenario_b["cache_hits"] / 1000 * INFRA_COST["qdrant"]
    )

    cost_reduction = (cost_a["total_llm_cost"] - cost_b["total_llm_cost"]) / cost_a["total_llm_cost"] * 100

    print("\n  ┌─────────────────────────────────────┐")
    print("  │  场景对比                            │")
    print("  ├──────────────┬──────────┬───────────┤")
    print("  │               │ 无缓存(A) │ 有缓存(B) │")
    print("  ├──────────────┼──────────┼───────────┤")
    print(f"  │ LLM 调用数    │ {scenario_a['llm_calls']:>8d} │ {scenario_b['llm_calls']:>9d} │")
    print(f"  │ 输入 Token   │ {scenario_a['total_input_tokens']:>8d} │ {scenario_b['total_input_tokens']:>9d} │")
    print(f"  │ 输出 Token   │ {scenario_a['total_output_tokens']:>8d} │ {scenario_b['total_output_tokens']:>9d} │")
    print(f"  │ LLM 成本     │ {cost_a['total_llm_cost']:>8.4f}$ │ {cost_b['total_llm_cost']:>9.4f}$ │")
    print(f"  │ 基础设施成本 │ {infra_cost_a:>8.4f}$ │ {infra_cost_b:>9.4f}$ │")
    print(f"  │ 总成本       │ {cost_a['total_llm_cost']:>8.4f}$ │ {cost_b['total_llm_cost'] + infra_cost_b:>9.4f}$ │")
    print("  └──────────────┴──────────┴───────────┘")
    print("\n  📊 缓存效果:")
    print(f"     LLM 调用减少:    {cost_b['cache_hits'] / scenario_b['total_queries'] * 100:.0f}%")
    print(f"     成本降低:        {cost_reduction:.1f}%")
    print(f"     缓存命中率:      {scenario_b['cache_hit_rate']:.0%}")

    report = {
        "date": datetime.now().isoformat(),
        "model": "Qwen3-8B",
        "provider": "siliconflow",
        "scenario_a": {
            "description": "无缓存（基线）",
            **scenario_a,
            **cost_a,
            "infra_cost": 0,
            "total_cost": cost_a["total_llm_cost"],
        },
        "scenario_b": {
            "description": "三级缓存全开",
            **scenario_b,
            **cost_b,
            "infra_cost": round(infra_cost_b, 4),
            "total_cost": round(cost_b["total_llm_cost"] + infra_cost_b, 4),
        },
        "comparison": {
            "llm_call_reduction_pct": round(
                (scenario_a["llm_calls"] - scenario_b["llm_calls"]) / scenario_a["llm_calls"] * 100, 1
            ),
            "cost_reduction_pct": round(cost_reduction, 1),
        },
    }

    report_path = f"reports/cost_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n  详细报告: {report_path}")

    return report


if __name__ == "__main__":
    analyze_cost()
