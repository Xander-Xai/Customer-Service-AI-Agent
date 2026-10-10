"""Agent Eval V1 — canonical contract（唯一真相源）。

与 ``scripts/eval_contract.py``（RAG 侧）同构：这里只放**数据与纯函数**，
不 import 应用模块，因此可以被 harness、CLI、CI 守卫与单测同时引用，
不产生循环依赖，也不允许出现第二份定义。

本模块同时是**证据边界的书面契约**。Agent Eval V1 的每个指标能证明什么、
不能证明什么，都写在 :data:`EVIDENCE_BOUNDARIES` 里，并被原样写进 evidence
artifact。理由：V1 用确定性脚本驱动 LLM，指标衡量的是**治理与编排层**的行为，
不是模型自身的决策质量；不把这句话写进 artifact，读者会把
"tool selection accuracy = 100%" 误读成"模型选工具选得准"。

被这个模块消灭的一类缺陷：过去"评测"结果只是 ``pytest`` 通过率。
通过率衡量的是**代码有没有按预期被写**，不是**任务有没有被完成**。两者的
分母、失败定义、证据形态都不同，混用等于宣称「测试通过 = Agent 能干活」。
"""

from __future__ import annotations

from typing import Any

DATASET_SCHEMA_VERSION = "agent-eval-dataset/v1"
#: 口径修订历史（三代数字**互不可比**）：
#:   v1   —— task_completion 只要求「抵达终态 + 非空回复」，交付兜底文案也算完成。
#:   v1.1 —— 拆出 workflow_execution / response_delivery / evidence_coverage；
#:           completion 要求「无未预期降级」+ expect_task_completed 标签。
#:   v1.2 —— **WAITING_APPROVAL 不再计入业务完成**：新增
#           governance_outcome_match_rate 承接"治理正确"的语义，并把
#           interrupt/pending_actions 从"业务完成证据"里剔除。
REPORT_SCHEMA_VERSION = "agent-eval-evidence/v1.2"

#: 与 ``core/hitl/risk.RiskLevel`` 同源的三档；不另立词汇表。
RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: 图在一次执行里可能抵达的终态。与 ``runtime/statuses.py`` 对齐，但只保留
#: harness 需要区分的几个：``SUCCEEDED`` / ``WAITING_APPROVAL`` / ``FAILED``。
TERMINAL_STATES: tuple[str, ...] = ("SUCCEEDED", "WAITING_APPROVAL", "FAILED")

#: 路由来源。**取自生产代码实际写入的字段**，不是本模块自造的分类：
#: ``router/query_router.py`` 把 ``"rule_shortcut"`` 写进
#: ``RoutingResult.raw_llm_result``；``core/graph_builder.py`` 另外可能写入
#: ``"[circuit_breaker_open]"`` / ``"[error_fallback]"`` / 原始模型文本。
ROUTE_SOURCES: tuple[str, ...] = (
    "rule_shortcut",
    "llm_arbitrated",
    "cached",
    "circuit_breaker_open",
    "error_fallback",
    "unavailable",
)

#: 唯一持有 ``tool_registry`` 的协作模式。只有落在它上面的 case 才会走工具循环，
#: 因此只有它能进入工具类指标的分母（不可达 case 必须显式排除并记账）。
TOOL_REACHABLE_MODE = "react"

#: 全部协作模式（与 ``collaboration/orchestrator.py::_modes`` 同源）。
#: 用于 orchestration-coverage 诊断：如实报告本数据集**没有**覆盖到哪些模式，
#: 而不是假装模式选择已被遍历。
COLLABORATION_MODES: tuple[str, ...] = (
    "sequential",
    "parallel",
    "consultation",
    "hierarchical",
    "react",
)

#: LangGraph 在 interrupt 时于返回值注入的键（与 ``core/hitl/gate.py`` 同源）。
WAITING_APPROVAL = "WAITING_APPROVAL"

#: ``agents/base_agent.py`` 高置信短路时的日志标记（生产代码里真实存在的字符串）。
ROUTE_SHORTCUT_MARKER = ("路由捷径", "rule_confidence")

#: 高置信规则短路阈值（与 ``router/query_router.py::route`` 里的字面量一致）。
ROUTE_SHORTCUT_CONFIDENCE = 0.75

#: ``agents/base_agent.py`` 摘出高风险动作时的日志标记。
HITL_DEFER_MARKER = ("高风险工具已摘出待人工审批", "本轮摘出")

#: 生产代码里**真实存在**的降级标记。
#:
#: 已 **收敛到唯一真相源** ``core.outcome.FALLBACK_MARKERS``：运行时的
#: ``core.monitoring`` / ``agents.response_agent`` 与评测侧的
#: ``fallback_rate`` / ``fallback_detection_accuracy`` 共用同一份清单，
#: 不再各存一份（两份清单一旦漂移，"同一份 artifact" 与 "运行时指标"
#: 会对同一条降级路径给出不同结论）。``evaluation`` 只做 re-export；契约
#: 由 ``tests/unit/test_agent_eval_contract.py`` 逐条回查生产源码守卫。
from core.outcome import FALLBACK_MARKERS  # noqa: E402  (re-export; single source)

# ── 指标名（唯一真相源）───────────────────────────────────────────────
# 正式指标 = 可以对外陈述的行为；诊断指标 = 有价值但覆盖面或语义受限。

METRIC_NAMES: tuple[str, ...] = (
    "route_accuracy",
    "tool_selection_accuracy",
    "tool_argument_schema_pass_rate",
    "forbidden_tool_rate",
    "hitl_trigger_accuracy",
    "workflow_execution_rate",
    "response_delivery_rate",
    "governance_outcome_match_rate",
    "task_completion_rate",
    "task_completion_evidence_coverage",
    "fallback_rate",
    "fallback_detection_accuracy",
)

DIAGNOSTIC_METRIC_NAMES: tuple[str, ...] = (
    "rule_route_agreement",
    "hitl_gate_propagation",
    "expected_parameter_match_rate",
    "step_count",
)

#: 每个指标能证明什么 / 不能证明什么。**逐字写进 evidence artifact**。
EVIDENCE_BOUNDARIES: dict[str, dict[str, str]] = {
    "route_accuracy": {
        "measures": (
            "确定性规则路径（rule_shortcut，LLM 未被调用）上，最终 query_type "
            "与人工标注一致的比例 —— 端到端、真实生产代码。"
        ),
        "does_not_measure": (
            "LLM 仲裁路径上的路由质量；那部分只进 rule_route_agreement 诊断视图，"
            "且衡量的是规则分类器而非 LLM。"
        ),
    },
    "rule_route_agreement": {
        "measures": "纯规则分类器输出与人工标注的一致率（含被 LLM 仲裁的 case）。",
        "does_not_measure": "端到端路由结果 —— 只有 rule_shortcut 子集才是端到端确定的。",
    },
    "tool_selection_accuracy": {
        "measures": (
            "给定脚本化的工具调用计划，编排层/工具循环是否执行了 expected_tools "
            "这一集合（含被 HITL 摘下的高风险调用）—— 即治理层保真度。"
        ),
        "does_not_measure": (
            "模型的工具选择能力。V1 的工具计划来自数据集而非模型，因此该指标是"
            "**治理层保真度**，不是 model capability。"
        ),
    },
    "tool_argument_schema_pass_rate": {
        "measures": (
            "实际发起的工具调用参数是否通过该工具在 ToolRegistry 里声明的 " "JSON Schema 子集校验。"
        ),
        "does_not_measure": (
            "参数语义正确性（keyword 传的是不是用户真要查的东西）—— "
            "那需要标注与 LLM 判断，V1 不做。"
        ),
    },
    "expected_parameter_match_rate": {
        "measures": (
            "实际发起的工具调用参数是否**含有**数据集为该工具声明的期望参数"
            "（`expected_parameters`，子集匹配）。这是参数**语义期望**的诊断校验，"
            "与 `tool_argument_schema_pass_rate` 的 schema 校验互补。"
        ),
        "does_not_measure": (
            "模型是否**自己**算出了正确参数。参数由脚本化 LLM 直接给出，因此它衡量的是"
            "「期望参数是否端到端被保留/传递」，不是模型的参数推理能力；也不覆盖未声明"
            "期望参数的工具。"
        ),
    },
    "forbidden_tool_rate": {
        "measures": "实际发起的工具调用中命中 forbidden_tools 的比例（目标为 0）。",
        "does_not_measure": "生产环境的权限边界。V1 没有真实 ERP 凭据，staging 工具只验证治理机制。",
    },
    "hitl_trigger_accuracy": {
        "measures": (
            "按 core.hitl.risk 策略应判定为 HIGH 的调用，是否真的被工具循环"
            "**摘出且未执行**（observed_gate 与 policy_gate 一致的比例）。"
        ),
        "does_not_measure": (
            "审批闸门端到端挂起/恢复（那是 hitl_gate_propagation 诊断指标）；"
            "也不涉及真实 ERP 写操作（NOT_VERIFIED）。"
        ),
    },
    "hitl_gate_propagation": {
        "measures": (
            "被摘出的高风险动作是否真的到达 state['pending_actions'] 并让图挂在 "
            "__interrupt__ 上（人工审批治理链路的端到端连通性）。"
        ),
        "does_not_measure": "审批决策与恢复执行（需真实 PostgreSQL，见 runtime-e2e lane）。",
    },
    "governance_outcome_match_rate": {
        "measures": (
            "图是否抵达了 expected_terminal_state —— **包含** WAITING_APPROVAL。高风险"
            "动作被正确摘出、图正确挂在 interrupt 上、run 停在 WAITING_APPROVAL，"
            "都属于**治理正确**。"
        ),
        "does_not_measure": (
            "业务是否完成。此时业务动作本来就**不该**执行，所以它只进治理口径，"
            "不进 task_completion_rate。把两者混同会让「正确地拦住」冒充「成功地退款」。"
        ),
    },
    "task_completion_rate": {
        "measures": (
            "业务任务是否真的完成：抵达期望终态 + 交付了非空回复 + **无未预期降级**。"
            "分母只含**业务完成口径适用**的 case；``WAITING_APPROVAL`` 的 case 被"
            "排除并计入 ``excluded``（它们按设计没有完成业务任务）。"
            "``expect_task_completed=false`` 的故障注入 case 留在分母、"
            "不进分子（如实拉低完成率）。"
        ),
        "does_not_measure": (
            "回答内容是否正确（需要人工/LLM 判定，V1 不做）；也不等于真实 LLM "
            "业务任务成功率 —— 回答由脚本化 LLM 产出，衡量的是编排/治理层行为。"
        ),
    },
    "workflow_execution_rate": {
        "measures": "图正常结束（未抛未捕获异常）的 case 占比 —— 工作流是否跑通。",
        "does_not_measure": "回复是否交付、业务问题是否解决（那是 delivery / completion 指标）。",
    },
    "response_delivery_rate": {
        "measures": "向用户交付了非空回复的 case 占比 —— 有没有东西交出去。",
        "does_not_measure": (
            "回复是否解决问题。交付了非空回复（哪怕是一句兜底文案）也算交付，"
            "所以 delivery 率恒高于 completion 率；交付 ≠ 解决。"
        ),
    },
    "task_completion_evidence_coverage": {
        "measures": (
            "被计为「已完成」的 case 中，完成结论有**独立可核验证据**（真正执行过并"
            "返回结果的工具调用）支撑的比例。"
        ),
        "does_not_measure": (
            "interrupt / pending_actions **不是**业务证据 —— 它只证明闸门拦住了高风险"
            "动作，不证明退款/改单真的执行了；脚本化 LLM 直接给出的文字回答同样不可"
            "独立核验。因此覆盖率低是**事实陈述**，不是缺陷。"
        ),
    },
    "fallback_rate": {
        "measures": "运行中命中了至少一个**生产日志里真实存在的降级标记**的 case 占比。",
        "does_not_measure": (
            "降级后的回答质量；也依赖标记清单的完备性 —— 新增降级路径而忘记登记标记"
            "会导致漏检，FALLBACK_MARKERS 必须与代码同步（由单测守卫）。"
        ),
    },
    "fallback_detection_accuracy": {
        "measures": "expect_fallback 标注与实测降级标记是否一致。",
        "does_not_measure": "降级本身是否正确（有些降级是正确的）。",
    },
    "step_count": {
        "measures": (
            "LangGraph 节点执行次数（astream updates 分块数）的分布，以及"
            "超出 case 自带 max_steps 预算的 case 数。"
        ),
        "does_not_measure": (
            "token 成本与真实 wall-clock；本仓库的 Step/Latency/Cost 三分口径里，"
            "V1 只覆盖 Step。"
        ),
    },
}


def metric_boundary(name: str) -> dict[str, str]:
    """取某个指标的证据边界；未知指标显式报错而不是返回空 dict。"""
    try:
        return EVIDENCE_BOUNDARIES[name]
    except KeyError:
        raise KeyError(f"unknown agent-eval metric: {name!r}") from None


def is_valid_risk(value: Any) -> bool:
    return value is None or value in RISK_LEVELS


def is_valid_terminal_state(value: Any) -> bool:
    return value in TERMINAL_STATES


def describe_contract() -> dict[str, Any]:
    """把契约本身导出成 artifact 的 ``contract`` 段（供读者离线核对）。"""
    return {
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "metric_names": list(METRIC_NAMES),
        "diagnostic_metric_names": list(DIAGNOSTIC_METRIC_NAMES),
        "evidence_boundaries": EVIDENCE_BOUNDARIES,
        "fallback_markers": {name: meaning for name, meaning in FALLBACK_MARKERS},
        "route_sources": list(ROUTE_SOURCES),
        "risk_levels": list(RISK_LEVELS),
        "terminal_states": list(TERMINAL_STATES),
        "tool_reachable_mode": TOOL_REACHABLE_MODE,
        "collaboration_modes": list(COLLABORATION_MODES),
        "route_shortcut_confidence": ROUTE_SHORTCUT_CONFIDENCE,
    }


__all__ = [
    "COLLABORATION_MODES",
    "DATASET_SCHEMA_VERSION",
    "DIAGNOSTIC_METRIC_NAMES",
    "EVIDENCE_BOUNDARIES",
    "FALLBACK_MARKERS",
    "HITL_DEFER_MARKER",
    "METRIC_NAMES",
    "REPORT_SCHEMA_VERSION",
    "RISK_LEVELS",
    "ROUTE_SHORTCUT_CONFIDENCE",
    "ROUTE_SHORTCUT_MARKER",
    "ROUTE_SOURCES",
    "TERMINAL_STATES",
    "TOOL_REACHABLE_MODE",
    "WAITING_APPROVAL",
    "describe_contract",
    "is_valid_risk",
    "is_valid_terminal_state",
    "metric_boundary",
]
