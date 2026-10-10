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
    COLLABORATION_MODES,
    DIAGNOSTIC_METRIC_NAMES,
    METRIC_NAMES,
    REPORT_SCHEMA_VERSION,
    describe_contract,
)
from .harness import CaseObservation
from .metrics import MEASURED, NOT_MEASURED, Metric, compute_metrics

PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
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
    "governance_outcome_match_rate": {"op": "gte", "threshold": 1.0},
    "task_completion_rate": {"op": "gte", "threshold": 0.95},
    "hitl_gate_propagation": {"op": "gte", "threshold": 1.0},
    "fallback_detection_accuracy": {"op": "gte", "threshold": 1.0},
    "step_count": {"op": "within_budget", "threshold": 1.0},
    # NOTE: ``route_accuracy`` 的门禁阈值是 0.80，但**在人工标注完成前它必然是
    # NOT_AVAILABLE**。它不会让整体变成 FAIL（那是系统的错），而是让整体变成
    # **INCONCLUSIVE** —— 「可测的门槛都过了，但有一项因为缺人工 ground truth
    # 根本没测出来」。
    #
    # INCONCLUSIVE **不等于 PASS**：artifact 的 overall_status 写的是
    # INCONCLUSIVE 而非 PASS，measurement_gaps 逐条列出未测出的门禁，CLI 也会
    # 打出显式警告。把 NOT_MEASURED 渲染成 PASS，等于让「没测」冒充「达标」。
    #
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


def overall_status_from_gates(gates: dict[str, str]) -> str:
    """把逐条门禁结果折叠成 overall_status（三态）。

    真值表（由 ``tests/unit/test_agent_eval_contract.py`` 穷举守卫）::

        存在 FAIL            -> FAIL            （可测门禁真的没过 = 系统缺陷）
        无 FAIL 但有 NOT_AVAILABLE -> INCONCLUSIVE（缺外部前提，不能宣称达标）
        全 PASS             -> PASS

    ``PASS`` 与"存在 NOT_AVAILABLE"**互斥**：没测出来的东西永远不能算达标。
    """
    if any(result == FAIL for result in gates.values()):
        return FAIL
    if any(result == NOT_AVAILABLE for result in gates.values()):
        return INCONCLUSIVE
    return PASS


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

    # overall_status 是三态，不是二态：
    #   FAIL         —— 至少一条**可测**门禁真的没过（系统问题）
    #   INCONCLUSIVE —— 可测门禁全过，但仍有门禁 NOT_AVAILABLE（缺外部前提，
    #                   例如人工标注），因此**不能**宣称整体达标
    #   PASS         —— 全部门禁 PASS 且无一条 NOT_AVAILABLE
    not_available_gates = sorted(name for name, v in gates.items() if v == NOT_AVAILABLE)
    overall_status = overall_status_from_gates(gates)

    measurement_gaps = [
        {
            "gate": name,
            "status": NOT_AVAILABLE,
            "reason": (metrics[name].notes if name in metrics else "metric not produced"),
            "unblocks_with": (
                "human-confirmed expected_route labels "
                "(scripts/approve_agent_eval_annotations.py)"
                if name == "route_accuracy"
                else None
            ),
        }
        for name in not_available_gates
    ]

    populations = dataset.population_counts()

    # Orchestration coverage: report what the dataset actually exercised and,
    # just as importantly, what it did NOT. Mode selection is not a metric here
    # (see harness: tool cases are pinned to ``react``), so coverage is an
    # honest diagnostic, not a score. Uncovered modes are surfaced explicitly so
    # nobody reads "governance_outcome_match_rate = 1.0" as "all 5 modes tested".
    mode_counts: dict[str, int] = {}
    single_agent_cases = 0
    multi_agent_cases = 0
    for o in observations:
        mode = o.observed_mode or "unknown"
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        if len(o.agents_used) == 1:
            single_agent_cases += 1
        elif len(o.agents_used) >= 2:
            multi_agent_cases += 1
    observed_modes = sorted(m for m in mode_counts if m != "unknown")
    uncovered_modes = [m for m in COLLABORATION_MODES if m not in observed_modes]
    orchestration_coverage = {
        "observed_mode_counts": dict(sorted(mode_counts.items())),
        "expected_modes": list(COLLABORATION_MODES),
        "covered_modes": observed_modes,
        "uncovered_modes": uncovered_modes,
        # A cross-agent hand-off requires >= 2 agents on one case.
        "cases_with_single_agent": single_agent_cases,
        "cases_with_multi_agent": multi_agent_cases,
        "measures": (
            "which collaboration modes and cross-agent hand-offs the dataset "
            "actually drove on the real graph"
        ),
        "does_not_measure": (
            "that every mode's internal correctness was verified. Tool cases are "
            "pinned to react; uncovered_modes are a declared coverage gap, not a "
            "pass. Mode *selection* quality needs a human-labelled route set."
        ),
    }

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": None,  # 由 CLI 填入，保持 build_report 纯函数
        "overall_status": overall_status,
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
        "orchestration_coverage": orchestration_coverage,
        "measurement_gaps": measurement_gaps,
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
            "real_provider": {
                "status": NOT_MEASURED,
                "reason": "no real provider credentials in this environment",
                "lanes": {
                    "scripted_llm_regression": "MEASURED (this artifact)",
                    "real_model_evaluation": NOT_MEASURED,
                },
                "unblock": (
                    "provide a provider credential and run a separate real-model lane; "
                    "its metrics must never be mixed with this scripted-LLM regression run"
                ),
            },
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
            "task_completion_rate additionally requires no unexpected degradation and "
            "excludes WAITING_APPROVAL cases, which are scored by "
            "governance_outcome_match_rate instead; fault-injection cases "
            "(expect_task_completed=false) never count as completions",
            "task_completion_evidence_coverage is the share of counted completions backed by "
            "an actually executed tool result; an interrupt / pending_action is governance "
            "evidence and is deliberately NOT counted as business-completion evidence",
            "overall_status is three-valued: FAIL (a measurable gate regressed), "
            "INCONCLUSIVE (measurable gates pass but some gate is NOT_AVAILABLE), PASS. "
            "NOT_MEASURED is never rendered as PASS.",
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
    "INCONCLUSIVE",
    "LEVEL_2_APPLICATION_MEASURED",
    "NOT_AVAILABLE",
    "PASS",
    "build_report",
    "diagnostic_metric_names",
    "formal_metric_names",
    "overall_status_from_gates",
    "write_report",
]
