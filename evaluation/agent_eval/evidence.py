"""Agent Eval V1 evidence artifact：构造、门禁判定、序列化。

artifact 里必须同时出现三样东西，缺一不可：

1. **指标与分母**（``metrics``）—— 分数本身；
2. **证据边界**（``contract.describe_contract()``）—— 每个分数能证明什么、
   不能证明什么；
3. **provenance**（``provenance``）—— 数据集 sha256、被测代码 sha、生成时间、
   被注入的替身是什么。

少了 (2)，读者会把"治理层保真度 100%"读成"模型选工具选得准"；少了 (3)，
artifact 就只是一串无法追溯到任何一次代码状态的数字。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .cases import AgentDataset
from .contract import (
    DIAGNOSTIC_METRIC_NAMES,
    METRIC_NAMES,
    REPORT_SCHEMA_VERSION,
    describe_contract,
)
from .harness import CaseObservation
from .metrics import MEASURED, Metric, compute_metrics

PASS = "PASS"
FAIL = "FAIL"
LEVEL_2_APPLICATION_MEASURED = "LEVEL_2_APPLICATION_MEASURED"
NOT_AVAILABLE = "NOT_AVAILABLE"

#: 门禁表。op 取值：``gte`` / ``lte`` / ``within_budget``。
#: 一条 ``NOT_MEASURED`` 的指标**不可能**让门禁通过 —— 没测出来不等于达标。
GATES: dict[str, dict[str, Any]] = {
    "route_accuracy": {"op": "gte", "threshold": 0.80},
    "tool_selection_accuracy": {"op": "gte", "threshold": 1.0},
    "tool_argument_schema_pass_rate": {"op": "gte", "threshold": 1.0},
    "forbidden_tool_rate": {"op": "lte", "threshold": 0.0},
    "hitl_trigger_accuracy": {"op": "gte", "threshold": 1.0},
    "workflow_execution_rate": {"op": "gte", "threshold": 1.0},
    "task_completion_rate": {"op": "gte", "threshold": 0.95},
    "hitl_gate_propagation": {"op": "gte", "threshold": 1.0},
    "fallback_detection_accuracy": {"op": "gte", "threshold": 1.0},
    "step_count": {"op": "within_budget", "threshold": 1.0},
    # NOTE: ``route_accuracy`` 的门禁阈值是 0.80，但**在人工标注完成前它必然是
    # NOT_AVAILABLE**，于是 overall_status 必然 FAIL。
    #
    # 这是刻意设计，不是缺陷：门禁不许靠「没测出来」过关。一个以「路由准确率」
    # 为卖点的评测集，在没有人工确认的 ground truth 时**就应该**报红 ——
    # 否则它报的其实是「LLM 给自己的标签打了多少分」。
    # 人工确认入口：scripts/approve_agent_eval_annotations.py
}


def _json_safe(value: Any) -> Any:
    """把不可 JSON 序列化的对象（LangGraph Interrupt 等）降级为 repr 文本。"""
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(v) for v in value]
    return repr(value)


def _gate_result(metric: Metric | None, spec: dict[str, Any]) -> str:
    if metric is None or metric.status != MEASURED or metric.value is None:
        # 没测出来就不能算达标。把 NOT_MEASURED 渲染成 PASS 会让门禁变成摆设。
        return NOT_AVAILABLE
    op = spec["op"]
    threshold = spec["threshold"]
    if op == "gte":
        return PASS if metric.value >= threshold else FAIL
    if op == "lte":
        return PASS if metric.value <= threshold else FAIL
    if op == "within_budget":
        within = metric.detail.get("within_budget")
        return PASS if within == metric.denominator else FAIL
    raise ValueError(f"unknown gate op {op!r}")


def build_report(
    *,
    dataset: AgentDataset,
    observations: Sequence[CaseObservation],
    run_id: str | None = None,
    git_sha: str | None = None,
    hitl_enabled: bool = False,
    staging_tools_registered: bool = True,
    extra_notes: Sequence[str] = (),
) -> dict[str, Any]:
    """构造 evidence artifact（纯函数，不落盘）。"""
    metrics = compute_metrics(dataset.cases, observations)
    gates = {name: _gate_result(metrics.get(name), spec) for name, spec in GATES.items()}
    passed = all(v == PASS for v in gates.values())
    populations = dataset.population_counts()

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": None,  # 由 CLI 填入，保持 build_report 纯函数
        "overall_status": PASS if passed else FAIL,
        "evidence_level": LEVEL_2_APPLICATION_MEASURED,
        "run_id": run_id,
        "git_sha": git_sha,
        "dataset": {
            "path": dataset.path,
            "sha256": dataset.sha256,
            "populations": populations,
            "case_count": len(dataset),
        },
        "configuration": {
            "hitl_enabled": hitl_enabled,
            "staging_tools_registered": staging_tools_registered,
        },
        "metrics": {name: metrics[name].to_dict() for name in sorted(metrics)},
        "gates": gates,
        "evidence_boundaries": describe_contract()["evidence_boundaries"],
        "contract": describe_contract(),
        "provenance": {
            "llm": [
                "evaluation.agent_eval.harness.ScriptedAgentLLM",
                "evaluation.agent_eval.harness.ScriptedRouterLLM",
            ],
            "provider": NOT_AVAILABLE,
            "network": NOT_AVAILABLE,
            "api_key": NOT_AVAILABLE,
            "nature": ("NONE — a scripted in-process double; no provider, no API key, no network"),
            "measures": ("orchestration / governance / routing behaviour of the real graph"),
            "does_not_measure": (
                "model capability (tool-choice quality, answer correctness, reasoning quality); "
                "LLM-as-a-Judge and real-provider lanes are explicitly out of V1 scope"
            ),
            "side_effects": (
                "side effects are verified against tools/hitl_staging_tools.py (deterministic "
                "local staging), NOT real ERP writes — the latter stays NOT_VERIFIED"
            ),
            "retrieval": (
                "this suite does not score retrieval quality; that is make rag-eval-649's job"
            ),
            "elapsed_ms": {
                "unit": "milliseconds",
                "measures": (
                    "wall-clock of an in-process single-run replay with no network; it measures "
                    "harness overhead, NOT production p50/p95. Do not quote it as SLO evidence."
                ),
                "value": NOT_AVAILABLE,
            },
        },
        "observations": [_json_safe(o.to_dict()) for o in observations],
        "extra_notes": [
            "every metric carries numerator / denominator / excluded; a metric whose "
            "denominator is 0 is reported as NOT_MEASURED, never as 0%",
            "metric names are defined once in evaluation/agent_eval/contract.py",
            "route_accuracy excludes LLM-arbitrated cases by design — see "
            "contract.evidence_boundaries.route_accuracy",
            "tool_selection_accuracy measures governance fidelity for a scripted plan, not model "
            "tool-choice quality",
            "delivery != completion: response_delivery_rate counts delivered replies; "
            "task_completion_rate additionally requires no unexpected degradation, and "
            "fault-injection cases (expect_task_completed=false) never count as completions",
            "task_completion_evidence_coverage is the share of counted completions backed by an "
            "executed tool result or WAITING_APPROVAL's pending_actions+interrupt; a scripted "
            "LLM's prose answer is not independently verifiable",
            "WAITING_APPROVAL counts as a correct governance outcome, not a completed business "
            "task; a delivered pre-interrupt response does not make the business task complete",
            *extra_notes,
        ],
    }


def formal_metric_names() -> tuple[str, ...]:
    return METRIC_NAMES


def diagnostic_metric_names() -> tuple[str, ...]:
    return DIAGNOSTIC_METRIC_NAMES


def write_report(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


__all__ = [
    "FAIL",
    "GATES",
    "LEVEL_2_APPLICATION_MEASURED",
    "NOT_AVAILABLE",
    "PASS",
    "build_report",
    "diagnostic_metric_names",
    "formal_metric_names",
    "write_report",
]
