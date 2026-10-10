"""Agent Eval V1 数据集：case 结构、严格校验、加载。

设计取舍
--------
* **JSONL 而不是 JSON 数组**：一条 case 一行，``git diff`` 能只显示改动的 case，
  也能像 RAG 侧的 ``tests/eval/queries/*.json`` 一样被按类别拆分后再 ``cat`` 合并。
* **严格校验、失败即崩**：数据集是评测的**分母来源**。一条语义不合法的 case
  静默跳过会让分母变小、分数变高 —— 那是最危险的评测缺陷，比 CI 变红坏得多。
  所以 loader 对每条 case 做全字段校验，任何违规直接抛 :class:`DatasetError`。
* **可选字段用 ``None`` 而不是"空列表"**：``expected_tools: []`` 表示"这个 case
  确实不该调任何工具"（是一个断言）；``expected_route: null`` 表示"这条 case 不参与
  路由评分"（是不参与评分）。两者语义不同，不能用同一个默认值表示。
* **标注来源必须显式**：``annotation`` 段记录每条 case 的
  ``provenance``（human_confirmed / llm_candidate）与 ``confirmed_by``。
  只有 ``human_confirmed`` 的 ``expected_route`` 会进入正式 ``route_accuracy``
  分母 —— 否则就成了「用 LLM 生成的标签去验证 LLM 驱动的系统」。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contract import (
    DATASET_SCHEMA_VERSION,
    RISK_LEVELS,
    TERMINAL_STATES,
    WAITING_APPROVAL,
    is_valid_risk,
    is_valid_terminal_state,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET_PATH = _PROJECT_ROOT / "tests" / "eval" / "agent_cases.jsonl"

_REQUIRED_FIELDS = (
    "schema_version",
    "case_id",
    "input",
    "expected_terminal_state",
    "max_steps",
)
_KNOWN_FIELDS = frozenset(
    {
        *_REQUIRED_FIELDS,
        "expected_route",
        "expected_tools",
        "forbidden_tools",
        "expected_risk",
        "expected_parameters",
        "scripted_tool_calls",
        "scripted_route",
        "scripted_failure",
        "expect_fallback",
        "expect_task_completed",
        "annotation",
        "tags",
        "notes",
    }
)

#: 故障注入（fault injection）用的脚本化失败。用于验证**降级路径**被正确触发，
#: 而不是验证「系统出错时会不会出错」。
SCRIPTED_FAILURES: tuple[str | None, ...] = (
    None,
    "agent_llm_error",
    "router_llm_error",
    "agent_llm_error_first_turn",
    "agent_llm_timeout",
)

#: 标注来源。``human_confirmed`` 才能进入正式路由指标。
ANNOTATION_PROVENANCE = ("human_confirmed", "llm_candidate")
ANNOTATION_METHODS = (
    "human_reviewed",
    "llm_generated_pending_human_review",
    "derived_from_code_contract",
)


class DatasetError(ValueError):
    """数据集不合契约。**不**降级、不跳过、不"尽量解析"。"""


@dataclass(frozen=True)
class ScriptedToolCall:
    name: str
    arguments_json: str

    @property
    def arguments(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.arguments_json)
        except json.JSONDecodeError as exc:  # pragma: no cover - loader 已挡
            raise DatasetError(f"tool call arguments must decode to an object: {exc}") from exc
        if not isinstance(parsed, dict):
            raise DatasetError("tool call arguments must be an object")
        return parsed


@dataclass(frozen=True)
class Annotation:
    """一条 case 的标注来源。

    ``provenance`` 是本仓库最重要的一条诚实性约定：

    * ``human_confirmed`` —— 有人真的看过并确认了 ``expected_route``；
    * ``llm_candidate`` —— LLM 生成的候选标签，**尚未**经人确认。

    只有前者能进入正式 ``route_accuracy`` 的分母。把两者混在一起，就等于
    「用自己生成的标签验证自己」，得到的准确率没有意义。
    """

    provenance: str = "llm_candidate"
    method: str = "llm_generated_pending_human_review"
    confirmed_by: str | None = None
    reviewed_at: str | None = None
    notes: str = ""

    @property
    def is_human_confirmed(self) -> bool:
        return self.provenance == "human_confirmed" and bool(self.confirmed_by)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provenance": self.provenance,
            "method": self.method,
            "confirmed_by": self.confirmed_by,
            "reviewed_at": self.reviewed_at,
            "notes": self.notes,
            "is_human_confirmed": self.is_human_confirmed,
        }


@dataclass(frozen=True)
class AgentCase:
    """一条确定性评测 case。"""

    case_id: str
    input: str
    expected_terminal_state: str
    max_steps: int
    expected_route: str | None = None
    expected_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    #: Correct arguments per tool (tool name -> expected argument subset). Nil for
    #: cases that carry no argument expectation. Cross-checked by the diagnostic
    #: ``expected_parameter_match_rate`` metric; it is NOT a schema check (that is
    #: ``tool_argument_schema_pass_rate``).
    expected_parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    expected_risk: str | None = None
    scripted_tool_calls: tuple[ScriptedToolCall, ...] = ()
    scripted_route: str | None = None
    scripted_failure: str | None = None
    expect_fallback: bool = False
    expect_task_completed: bool = True
    annotation: Annotation = field(default_factory=Annotation)
    tags: tuple[str, ...] = ()
    notes: str = ""

    def scores_route(self) -> bool:
        """该 case 是否进入路由指标分母。"""
        return self.expected_route is not None and self.annotation.is_human_confirmed

    def scores_tools(self) -> bool:
        """该 case 是否进入工具指标分母。"""
        return bool(self.expected_tools)

    def scores_risk(self) -> bool:
        """该 case 是否进入 HITL 触发指标分母。"""
        return self.expected_risk is not None and bool(self.scripted_tool_calls)

    def scores_parameters(self) -> bool:
        """该 case 是否进入期望参数匹配诊断指标的分母。"""
        return bool(self.expected_parameters)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "input": self.input,
            "expected_terminal_state": self.expected_terminal_state,
            "max_steps": self.max_steps,
            "expected_route": self.expected_route,
            "expected_tools": list(self.expected_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "expected_parameters": {
                name: dict(args) for name, args in self.expected_parameters.items()
            },
            "expected_risk": self.expected_risk,
            "scripted_tool_calls": [
                {"name": c.name, "arguments_json": c.arguments_json}
                for c in self.scripted_tool_calls
            ],
            "scripted_route": self.scripted_route,
            "scripted_failure": self.scripted_failure,
            "expect_fallback": self.expect_fallback,
            "expect_task_completed": self.expect_task_completed,
            "annotation": self.annotation.to_dict(),
            "tags": list(self.tags),
            "notes": self.notes,
            "scores_route": self.scores_route(),
            "scores_tools": self.scores_tools(),
            "scores_risk": self.scores_risk(),
        }


def _require_str(payload: dict[str, Any], key: str, line_no: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"line {line_no}: field `{key}` must be a non-empty string")
    return value


def _require_str_tuple(payload: dict[str, Any], key: str, line_no: int) -> tuple[str, ...]:
    raw = payload.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(x, str) or not x.strip() for x in raw):
        raise DatasetError(f"line {line_no}: field `{key}` must be a list of non-empty strings")
    return tuple(raw)


def _require_bool(payload: dict[str, Any], key: str, line_no: int, default: bool) -> bool:
    raw = payload.get(key, default)
    if not isinstance(raw, bool):
        raise DatasetError(f"line {line_no}: field `{key}` must be a boolean")
    return raw


def _parse_scripted_calls(raw: Any, line_no: int) -> tuple[ScriptedToolCall, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DatasetError(f"line {line_no}: field `scripted_tool_calls` must be a list")
    parsed: list[ScriptedToolCall] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise DatasetError(f"line {line_no}: scripted_tool_calls[{index}] must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise DatasetError(f"line {line_no}: scripted_tool_calls[{index}].name is required")
        args = item.get("arguments", {})
        if isinstance(args, str):
            arguments_json = args
        elif isinstance(args, dict):
            arguments_json = json.dumps(args, ensure_ascii=False, sort_keys=True)
        else:
            raise DatasetError(
                f"line {line_no}: scripted_tool_calls[{index}].arguments must be an object or JSON text"
            )
        try:
            decoded = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            raise DatasetError(
                f"line {line_no}: scripted_tool_calls[{index}].arguments is not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise DatasetError(
                f"line {line_no}: scripted_tool_calls[{index}].arguments must decode to an object"
            )
        parsed.append(ScriptedToolCall(name=name, arguments_json=arguments_json))
    return tuple(parsed)


def _parse_expected_parameters(raw: Any, line_no: int) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DatasetError(f"line {line_no}: field `expected_parameters` must be an object")
    parsed: dict[str, dict[str, Any]] = {}
    for name, args in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise DatasetError(
                f"line {line_no}: expected_parameters keys must be tool names (non-empty strings)"
            )
        if not isinstance(args, dict):
            raise DatasetError(
                f"line {line_no}: expected_parameters[{name!r}] must be an argument object"
            )
        parsed[name] = dict(args)
    return parsed


def _parse_annotation(raw: Any, line_no: int) -> Annotation:
    if raw is None:
        return Annotation()
    if not isinstance(raw, dict):
        raise DatasetError(f"line {line_no}: field `annotation` must be an object")
    provenance = raw.get("provenance", "llm_candidate")
    if provenance not in ANNOTATION_PROVENANCE:
        raise DatasetError(
            f"line {line_no}: annotation.provenance must be one of {list(ANNOTATION_PROVENANCE)}"
        )
    method = raw.get("method", "llm_generated_pending_human_review")
    if method not in ANNOTATION_METHODS:
        raise DatasetError(
            f"line {line_no}: annotation.method must be one of {list(ANNOTATION_METHODS)}"
        )
    confirmed_by = raw.get("confirmed_by")
    if provenance == "human_confirmed" and not confirmed_by:
        # 声称「已人工确认」却没有确认人 = 无法复核的断言，直接拒绝。
        raise DatasetError(
            f"line {line_no}: annotation.provenance=human_confirmed requires `confirmed_by`"
        )
    return Annotation(
        provenance=provenance,
        method=method,
        confirmed_by=confirmed_by,
        reviewed_at=raw.get("reviewed_at"),
        notes=str(raw.get("notes", "")),
    )


def parse_case(payload: dict[str, Any], line_no: int) -> AgentCase:
    """把一行 JSON 解析成 :class:`AgentCase`，任何违规都抛 :class:`DatasetError`。"""
    if not isinstance(payload, dict):
        raise DatasetError(f"line {line_no}: each dataset line must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version != DATASET_SCHEMA_VERSION:
        raise DatasetError(
            f"line {line_no}: schema_version {schema_version!r} != {DATASET_SCHEMA_VERSION!r}"
        )
    missing = [f for f in _REQUIRED_FIELDS if f not in payload]
    if missing:
        raise DatasetError(f"line {line_no}: missing required fields {missing}")
    unknown = sorted(set(payload) - _KNOWN_FIELDS)
    if unknown:
        raise DatasetError(f"line {line_no}: unknown fields {unknown}")

    max_steps = payload.get("max_steps")
    if not isinstance(max_steps, int) or max_steps <= 0:
        raise DatasetError(f"line {line_no}: `max_steps` must be a positive integer")

    terminal_state = _require_str(payload, "expected_terminal_state", line_no)
    if not is_valid_terminal_state(terminal_state):
        raise DatasetError(
            f"line {line_no}: expected_terminal_state {terminal_state!r} is not one of "
            f"{list(TERMINAL_STATES)}"
        )

    expected_risk = payload.get("expected_risk")
    if not is_valid_risk(expected_risk):
        raise DatasetError(
            f"line {line_no}: expected_risk {expected_risk!r} must be null or one of "
            f"{list(RISK_LEVELS)}"
        )

    scripted_failure = payload.get("scripted_failure")
    if scripted_failure not in SCRIPTED_FAILURES:
        raise DatasetError(
            f"line {line_no}: scripted_failure {scripted_failure!r} must be one of "
            f"{[x for x in SCRIPTED_FAILURES if x]}"
        )

    scripted_route = payload.get("scripted_route")
    if scripted_route is not None and (
        not isinstance(scripted_route, str) or not scripted_route.strip()
    ):
        raise DatasetError(f"line {line_no}: `scripted_route` must be null or a non-empty string")

    expected_route = payload.get("expected_route")
    if expected_route is not None and (
        not isinstance(expected_route, str) or not expected_route.strip()
    ):
        raise DatasetError(f"line {line_no}: `expected_route` must be null or a non-empty string")

    case = AgentCase(
        case_id=_require_str(payload, "case_id", line_no),
        input=_require_str(payload, "input", line_no),
        expected_terminal_state=terminal_state,
        max_steps=max_steps,
        expected_route=expected_route,
        expected_tools=_require_str_tuple(payload, "expected_tools", line_no),
        forbidden_tools=_require_str_tuple(payload, "forbidden_tools", line_no),
        expected_parameters=_parse_expected_parameters(payload.get("expected_parameters"), line_no),
        expected_risk=expected_risk,
        scripted_tool_calls=_parse_scripted_calls(payload.get("scripted_tool_calls"), line_no),
        scripted_route=scripted_route,
        scripted_failure=scripted_failure,
        expect_fallback=_require_bool(payload, "expect_fallback", line_no, False),
        expect_task_completed=_require_bool(payload, "expect_task_completed", line_no, True),
        annotation=_parse_annotation(payload.get("annotation"), line_no),
        tags=_require_str_tuple(payload, "tags", line_no),
        notes=str(payload.get("notes", "")),
    )
    _validate_semantics(case, line_no)
    return case


def _validate_semantics(case: AgentCase, line_no: int) -> None:
    """跨字段一致性校验（单字段合法 ≠ 组合起来讲得通）。"""
    if case.expected_tools and not case.scripted_tool_calls:
        raise DatasetError(
            f"line {line_no}: case {case.case_id!r} declares expected_tools "
            f"{list(case.expected_tools)} but has no scripted_tool_calls — "
            "工具选择指标会恒为 0 分（不可达 case 不会被静默排除）"
        )
    scripted_names = [c.name for c in case.scripted_tool_calls]
    if scripted_names and not case.expected_tools and not case.expect_fallback:
        raise DatasetError(
            f"line {line_no}: case {case.case_id!r} scripts tool calls {scripted_names} but "
            "declares no expected_tools (allowed only for expect_fallback fault-injection cases)"
        )
    bad = set(case.expected_tools) & set(case.forbidden_tools)
    if bad:
        raise DatasetError(
            f"line {line_no}: case {case.case_id!r} both expects and forbids {sorted(bad)}"
        )
    scripted_forbidden = set(scripted_names) & set(case.forbidden_tools)
    if scripted_forbidden:
        raise DatasetError(
            f"line {line_no}: case {case.case_id!r} scripts forbidden tools {sorted(scripted_forbidden)}; "
            "the scripted plan would make forbidden_tool_rate non-zero by construction, which "
            "measures the dataset rather than the system"
        )
    if case.expect_fallback and not case.notes.strip():
        raise DatasetError(
            f"line {line_no}: a fault-injection case must document in `notes` why the scripted plan "
            "is expected to stay unused"
        )
    if case.expected_risk is not None and not case.scripted_tool_calls:
        raise DatasetError(
            f"line {line_no}: case {case.case_id!r} declares expected_risk "
            f"{case.expected_risk!r} without any scripted tool call"
        )
    if case.expected_parameters:
        if not case.expected_tools:
            raise DatasetError(
                f"line {line_no}: case {case.case_id!r} declares expected_parameters without "
                "expected_tools — a parameter expectation needs a tool expectation to attach to"
            )
        unknown_tools = sorted(set(case.expected_parameters) - set(case.expected_tools))
        if unknown_tools:
            raise DatasetError(
                f"line {line_no}: case {case.case_id!r} expects parameters for tools "
                f"{unknown_tools} that are not in expected_tools {list(case.expected_tools)}"
            )
        if not case.scripted_tool_calls:
            raise DatasetError(
                f"line {line_no}: case {case.case_id!r} declares expected_parameters without "
                "scripted_tool_calls — the parameters can never be observed"
            )
    if (
        case.expected_terminal_state == WAITING_APPROVAL
        and case.expected_risk != "high"
        and case.expected_risk is not None
    ):
        raise DatasetError(
            f"line {line_no}: case {case.case_id!r} expects {WAITING_APPROVAL}, which only HIGH-risk "
            f"proposals can trigger, but expected_risk is {case.expected_risk!r}"
        )


@dataclass(frozen=True)
class AgentDataset:
    """一次加载的完整数据集 + 其内容指纹。"""

    path: str
    sha256: str
    cases: tuple[AgentCase, ...]

    def __len__(self) -> int:
        return len(self.cases)

    def by_id(self, case_id: str) -> AgentCase:
        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise KeyError(case_id)

    def select(self, case_ids: tuple[str, ...]) -> tuple[AgentCase, ...]:
        wanted = set(case_ids)
        unknown = wanted - {c.case_id for c in self.cases}
        if unknown:
            raise DatasetError(f"unknown case ids requested: {sorted(unknown)}")
        return tuple(c for c in self.cases if c.case_id in wanted)

    def population_counts(self) -> dict[str, int]:
        """各指标分母的 case 数（运行前即可核对，不用先跑一遍）。"""
        return {
            "total": len(self.cases),
            "route": sum(1 for c in self.cases if c.scores_route()),
            "tools": sum(1 for c in self.cases if c.scores_tools()),
            "parameters": sum(1 for c in self.cases if c.scores_parameters()),
            "risk": sum(1 for c in self.cases if c.scores_risk()),
            "waiting_approval": sum(
                1 for c in self.cases if c.expected_terminal_state == WAITING_APPROVAL
            ),
            "human_confirmed": sum(1 for c in self.cases if c.annotation.is_human_confirmed),
            "llm_candidate": sum(1 for c in self.cases if not c.annotation.is_human_confirmed),
        }


def _display_path(path: Path) -> str:
    """artifact 里记录的路径：仓库内就写相对路径，避免把本机绝对路径写进证据。"""
    try:
        return str(path.relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(path)


def load_dataset(path: Path | str | None = None) -> AgentDataset:
    """加载并全量校验数据集。文件不存在 / 非法行 / 重复 case_id 都抛错。"""
    dataset_path = Path(path) if path is not None else DEFAULT_DATASET_PATH
    if not dataset_path.exists():
        raise DatasetError(f"dataset not found: {_display_path(dataset_path)}")

    raw_text = dataset_path.read_text(encoding="utf-8")
    cases: list[AgentCase] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(raw_text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"line {line_no}: invalid JSON: {exc}") from exc
        case = parse_case(payload, line_no)
        if case.case_id in seen:
            raise DatasetError(f"line {line_no}: duplicate case_id {case.case_id!r}")
        seen.add(case.case_id)
        cases.append(case)

    if not cases:
        raise DatasetError(f"dataset is empty: {_display_path(dataset_path)}")

    return AgentDataset(
        path=_display_path(dataset_path),
        sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        cases=tuple(cases),
    )


__all__ = [
    "ANNOTATION_METHODS",
    "ANNOTATION_PROVENANCE",
    "AgentCase",
    "AgentDataset",
    "Annotation",
    "DATASET_SCHEMA_VERSION",
    "DEFAULT_DATASET_PATH",
    "DatasetError",
    "RISK_LEVELS",
    "SCRIPTED_FAILURES",
    "ScriptedToolCall",
    "TERMINAL_STATES",
    "load_dataset",
    "parse_case",
]
