#!/usr/bin/env python3
"""Token 成本分析脚本（基于 A/B 测试结果进行成本对比分析）。

可独立运行或依赖 benchmark_ab_test.py 的输出。

用法:
    python3 scripts/benchmark_cost.py                                           # 独立运行（默认定价 1000 queries）
    python3 scripts/benchmark_cost.py --input reports/ab_test_20260624_120000.json   # 基于已有 AB 测试报告
"""

import argparse
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

# 默认场景（无 --input 时使用）
_DEFAULT_SCENARIO_A = {
    "total_queries": 1000,
    "llm_calls": 1000,
    "total_input_tokens": 350000,
    "total_output_tokens": 100000,
}
_DEFAULT_SCENARIO_B = {
    "total_queries": 1000,
    "llm_calls": 350,
    "cache_hits": 650,
    "total_input_tokens": 122500,
    "total_output_tokens": 35000,
    "cache_hit_rate": 0.65,
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


def load_from_ab_report(path: str) -> tuple[dict, dict]:
    """从 benchmark_ab_test.py 输出的 JSON 报告加载场景数据"""
    with open(path, encoding="utf-8") as f:
        ab_report = json.load(f)

    va = ab_report.get("variant_a", {})
    vb = ab_report.get("variant_b", {})

    scenario_a = {
        "total_queries": va.get("total_queries", 1000),
        "llm_calls": va.get("llm_calls", 1000),
        "total_input_tokens": va.get("total_input_tokens", 350000),
        "total_output_tokens": va.get("total_output_tokens", 100000),
    }
    scenario_b = {
        "total_queries": vb.get("total_queries", 1000),
        "llm_calls": vb.get("llm_calls", 350),
        "cache_hits": vb.get("cache_hits", 650),
        "total_input_tokens": vb.get("total_input_tokens", 122500),
        "total_output_tokens": vb.get("total_output_tokens", 35000),
        "cache_hit_rate": vb.get("cache_hit_rate", 0.65),
    }
    print(f"  从 AB 测试报告加载数据: {path}")
    print(f"  变体 A: {scenario_a['llm_calls']} LLM调用, {scenario_a['total_input_tokens']+scenario_a['total_output_tokens']} tokens")
    print(f"  变体 B: {scenario_b['llm_calls']} LLM调用, {scenario_b['cache_hits']} 缓存命中, "
          f"{scenario_b['total_input_tokens']+scenario_b['total_output_tokens']} tokens")
    return scenario_a, scenario_b


def analyze_cost(input_path: str = None):
    """执行成本分析"""
    PROJECT_ROOT = Path(__file__).parent.parent

    print("=" * 60)
    print("  Token 成本分析报告")
    print("  Qwen3-8B @ 硅基流动")
    print("=" * 60)

    # 加载场景数据
    if input_path:
        scenario_a, scenario_b = load_from_ab_report(input_path)
    else:
        print("  使用默认场景数据（1000 queries, 65% 缓存命中率）")
        print("  提示: 使用 --input reports/ab_test_*.json 加载真实 AB 测试数据")
        scenario_a = dict(_DEFAULT_SCENARIO_A)
        scenario_b = dict(_DEFAULT_SCENARIO_B)

    cost_a = calculate_total_cost(scenario_a["total_input_tokens"], scenario_a["total_output_tokens"])
    cost_b = calculate_total_cost(scenario_b["total_input_tokens"], scenario_b["total_output_tokens"])

    # 基础设施成本
    infra_cost_a = 0  # 无缓存，无基础设施开销
    infra_cost_b = (
        scenario_b["cache_hits"] / 1000 * INFRA_COST["redis"] +
        scenario_b["cache_hits"] / 1000 * INFRA_COST["qdrant"]
    )

    total_a = cost_a["total_llm_cost"] + infra_cost_a
    total_b = cost_b["total_llm_cost"] + infra_cost_b
    cost_reduction = (total_a - total_b) / total_a * 100 if total_a > 0 else 0

    print("\n  ┌─────────────────────────────────────┐")
    print("  │  场景对比                            │")
    print("  ├──────────────┬──────────┬───────────┤")
    print("  │               │ 无缓存(A) │ 有缓存(B) │")
    print("  ├──────────────┼──────────┼───────────┤")
    print(f"  │ 查询数       │ {scenario_a['total_queries']:>8d} │ {scenario_b['total_queries']:>9d} │")
    print(f"  │ LLM 调用数    │ {scenario_a['llm_calls']:>8d} │ {scenario_b['llm_calls']:>9d} │")
    print(f"  │ 缓存命中数   │ {0:>8d} │ {scenario_b['cache_hits']:>9d} │")
    print(f"  │ 输入 Token   │ {scenario_a['total_input_tokens']:>8d} │ {scenario_b['total_input_tokens']:>9d} │")
    print(f"  │ 输出 Token   │ {scenario_a['total_output_tokens']:>8d} │ {scenario_b['total_output_tokens']:>9d} │")
    print(f"  │ LLM 成本     │ {cost_a['total_llm_cost']:>8.4f}$ │ {cost_b['total_llm_cost']:>9.4f}$ │")
    print(f"  │ 基础设施成本 │ {infra_cost_a:>8.4f}$ │ {infra_cost_b:>9.4f}$ │")
    print(f"  │ 总成本       │ {total_a:>8.4f}$ │ {total_b:>9.4f}$ │")
    print("  └──────────────┴──────────┴───────────┘")

    llm_reduction = (scenario_a["llm_calls"] - scenario_b["llm_calls"]) / scenario_a["llm_calls"] * 100
    print("\n  📊 缓存效果:")
    print(f"     LLM 调用减少:    {llm_reduction:.1f}%")
    print(f"     成本降低:        {cost_reduction:.1f}%")
    print(f"     缓存命中率:      {scenario_b.get('cache_hit_rate', 0):.0%}")

    report = {
        "date": datetime.now().isoformat(),
        "model": "Qwen3-8B",
        "provider": "siliconflow",
        "data_source": input_path or "default_scenario",
        "scenario_a": {
            "description": "无缓存（基线）",
            **scenario_a,
            **cost_a,
            "infra_cost": round(infra_cost_a, 4),
            "total_cost": round(total_a, 4),
        },
        "scenario_b": {
            "description": "三级缓存全开",
            **scenario_b,
            **cost_b,
            "infra_cost": round(infra_cost_b, 4),
            "total_cost": round(total_b, 4),
        },
        "comparison": {
            "llm_call_reduction_pct": round(llm_reduction, 1),
            "cost_reduction_pct": round(cost_reduction, 1),
        },
    }

    report_dir = PROJECT_ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"cost_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n  详细报告: {report_path}")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Token 成本分析")
    parser.add_argument(
        "--input", "-i",
        help="benchmark_ab_test.py 输出的 JSON 报告路径（省略时使用默认场景数据）",
    )
    args = parser.parse_args()
    analyze_cost(input_path=args.input)