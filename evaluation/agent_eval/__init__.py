"""Agent Eval V1：确定性 Agent 行为评测（真实图 + 脚本化 LLM）。

**它测什么、不测什么**见 :mod:`evaluation.agent_eval.contract` 的
``EVIDENCE_BOUNDARIES``，并被逐字写进每个 evidence artifact。

一句话版本：本包衡量**编排与治理层**的行为（路由、工具执行保真度、HITL 触发与
闸门连通性、降级路径、步数），**不**衡量模型的决策质量。任务成功率 ≠
``pytest`` 通过率 —— 后者的分母是"代码有没有按预期被写"。
"""

from .cases import AgentCase, AgentDataset, Annotation, DatasetError, load_dataset
from .contract import (
    DATASET_SCHEMA_VERSION,
    DIAGNOSTIC_METRIC_NAMES,
    EVIDENCE_BOUNDARIES,
    METRIC_NAMES,
    REPORT_SCHEMA_VERSION,
    describe_contract,
    metric_boundary,
)
from .evidence import build_report, write_report
from .harness import AgentEvalHarness, CaseObservation, ScriptedAgentLLM, ScriptedRouterLLM
from .metrics import MEASURED, NOT_MEASURED, Metric, compute_metrics

__all__ = [
    "AgentCase",
    "AgentDataset",
    "AgentEvalHarness",
    "Annotation",
    "CaseObservation",
    "DATASET_SCHEMA_VERSION",
    "DIAGNOSTIC_METRIC_NAMES",
    "DatasetError",
    "EVIDENCE_BOUNDARIES",
    "MEASURED",
    "METRIC_NAMES",
    "Metric",
    "NOT_MEASURED",
    "REPORT_SCHEMA_VERSION",
    "ScriptedAgentLLM",
    "ScriptedRouterLLM",
    "build_report",
    "compute_metrics",
    "describe_contract",
    "load_dataset",
    "metric_boundary",
    "write_report",
]
