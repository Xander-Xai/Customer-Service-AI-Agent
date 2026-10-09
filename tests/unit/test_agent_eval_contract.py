"""Agent Eval V1 契约守卫：指标名、边界、标记清单必须与生产代码同步。

这些断言存在的理由不是"代码风格"，而是**评测系统最容易悄悄说谎的三种方式**：

1. **指标名被复制成第二份**。artifact 里出现的名字必须来自
   ``contract.METRIC_NAMES``，否则读者手上的 artifact 与代码里的定义可能对不上。
2. **证据边界被删掉或弱化**。每个指标"不证明什么"是本套件的核心内容；
   少了它，"治理层保真度 100%"会被读成"模型选工具选得准"。
3. **降级标记清单与生产代码脱节**。新增一条降级路径却忘记登记，
   ``fallback_rate`` 会静默漏检；登记了但生产里根本没有这条路径，则是反向撒谎。
   因此本文件**逐条回查生产源码**：每条标记都必须在真实代码里 grep 得到。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluation.agent_eval import contract

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


class TestMetricNamesAreDefinedOnce:
    def test_formal_and_diagnostic_do_not_overlap(self):
        assert not set(contract.METRIC_NAMES) & set(contract.DIAGNOSTIC_METRIC_NAMES)

    def test_every_declared_metric_has_an_evidence_boundary(self):
        for name in contract.METRIC_NAMES + contract.DIAGNOSTIC_METRIC_NAMES:
            boundary = contract.metric_boundary(name)
            assert boundary["measures"].strip(), f"{name} 没有写明它测什么"
            assert boundary[
                "does_not_measure"
            ].strip(), f"{name} 没有写明它**不**测什么 —— 这是本套件最容易被误读的地方"

    def test_unknown_metric_raises_instead_of_returning_empty(self):
        with pytest.raises(KeyError):
            contract.metric_boundary("totally_made_up_metric")

    def test_compute_metrics_covers_exactly_the_declared_names(self):
        """compute_metrics 产出的键必须与契约完全一致（不多不少）。"""
        from evaluation.agent_eval.metrics import compute_metrics

        produced = set(compute_metrics.__code__.co_consts[-1:][0] if False else [])
        # 直接跑一遍空输入更可靠：全部分母为 0 -> 全是 NOT_MEASURED，但键一定齐。
        from evaluation.agent_eval.harness import CaseObservation

        obs = CaseObservation(
            case_id="x",
            nodes_executed=(),
            step_count=0,
            observed_route=None,
            route_source="unavailable",
            rule_route=None,
            rule_confidence=0.0,
            router_llm_calls=0,
            observed_mode=None,
            resolution_status=None,
            response="",
            terminal_state="FAILED",
            tool_calls=(),
            fallback_markers=(),
            route_shortcut_used=False,
            hitl_defer_logged=False,
            pending_actions=(),
            interrupt_payloads=(),
            side_effect_counters={},
            scripted_plan_remaining=0,
            scripted_tool_rounds=0,
            elapsed_ms=0.0,
        )
        from evaluation.agent_eval.cases import AgentCase

        case = AgentCase(case_id="x", input="q", expected_terminal_state="SUCCEEDED", max_steps=5)
        metrics = compute_metrics([case], [obs])
        assert set(metrics) == set(contract.METRIC_NAMES) | set(contract.DIAGNOSTIC_METRIC_NAMES)
        assert produced == set()  # 仅为消歧义，实际断言在上面


class TestEmptyDenominatorIsNeverZeroPercent:
    """分母为 0 必须是 NOT_MEASURED，不是 0%。"""

    def test_all_metrics_unmeasured_on_empty_population(self):
        """**零** case 时全部 NOT_MEASURED。"""
        from evaluation.agent_eval.metrics import MEASURED, NOT_MEASURED, compute_metrics

        metrics = compute_metrics([], [])
        for name, metric in metrics.items():
            if name == "step_count":
                continue
            assert metric.value is None, f"{name} 在零样本时报成了 {metric.value}"
            assert metric.status == NOT_MEASURED, f"{name}: {metric.status}"
            assert metric.status != MEASURED

    def test_a_real_miss_is_zero_not_not_measured(self):
        """一次真实的失败必须报 0 分，而不是 NOT_MEASURED。

        两者的区别是致命的：0 分是"跑了、没过"，NOT_MEASURED 是"没跑"。
        把前者渲染成后者，就等于把失败藏起来。
        """
        from evaluation.agent_eval.cases import AgentCase
        from evaluation.agent_eval.harness import CaseObservation
        from evaluation.agent_eval.metrics import MEASURED, compute_metrics

        obs = CaseObservation(
            case_id="x",
            nodes_executed=("check_cache",),
            step_count=1,
            observed_route="general",
            route_source="rule_shortcut",
            rule_route=None,
            rule_confidence=0.9,
            router_llm_calls=0,
            observed_mode="sequential",
            resolution_status=None,
            response="",
            terminal_state="FAILED",
            tool_calls=(),
            fallback_markers=(),
            route_shortcut_used=True,
            hitl_defer_logged=False,
            pending_actions=(),
            interrupt_payloads=(),
            side_effect_counters={},
            scripted_plan_remaining=0,
            scripted_tool_rounds=0,
            elapsed_ms=1.0,
        )
        case = AgentCase(
            case_id="x",
            input="q",
            expected_terminal_state="SUCCEEDED",
            max_steps=5,
            expected_route="general",
        )
        metrics = compute_metrics([case], [obs])
        assert metrics["task_completion_rate"].status == MEASURED
        assert metrics["task_completion_rate"].value == 0.0
        assert metrics["task_completion_rate"].denominator == 1


class TestFallbackMarkersExistInProductionCode:
    """每条降级标记都必须在生产源码里真实存在。"""

    SEARCH_ROOTS = ("agents", "core", "collaboration", "router", "llm", "cache")

    def _corpus(self) -> str:
        parts = []
        for root in self.SEARCH_ROOTS:
            for path in (REPO_ROOT / root).rglob("*.py"):
                parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        return "\n".join(parts)

    def test_every_marker_is_found_in_production_source(self):
        corpus = self._corpus()
        missing = [marker for marker, _ in contract.FALLBACK_MARKERS if marker not in corpus]
        assert not missing, (
            f"这些降级标记在生产代码里 grep 不到：{missing}。"
            " 登记一个不存在的标记等于凭空捏造一条降级路径。"
        )

    def test_every_marker_is_non_trivial(self):
        for marker, _meaning in contract.FALLBACK_MARKERS:
            assert len(marker) >= 6, f"标记 {marker!r} 过短，容易误命中"


class TestSchemaSubsetIsHonest:
    def test_annotation_only_keywords_are_ignored(self):
        from evaluation.agent_eval.schema_check import validate_arguments

        schema = {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号"}},
            "required": ["order_id"],
        }
        assert validate_arguments({"order_id": "O-1"}, schema) == []

    def test_semantic_keywords_outside_the_subset_raise(self):
        """带语义但子集不支持的关键字必须**报错**，不能悄悄跳过校验。"""
        from evaluation.agent_eval.schema_check import SchemaError, validate_arguments

        schema = {
            "type": "object",
            "properties": {"code": {"type": "string", "pattern": "^[A-Z]{2}$"}},
        }
        with pytest.raises(SchemaError):
            validate_arguments({"code": "abc"}, schema)


class TestDatasetLoaderIsStrict:
    def test_unknown_field_is_rejected(self):
        from evaluation.agent_eval.cases import DatasetError, parse_case

        payload = {
            "schema_version": contract.DATASET_SCHEMA_VERSION,
            "case_id": "c1",
            "input": "q",
            "expected_terminal_state": "SUCCEEDED",
            "max_steps": 5,
            "surprise": True,
        }
        with pytest.raises(DatasetError, match="unknown fields"):
            parse_case(payload, 1)

    def test_human_confirmed_without_reviewer_is_rejected(self):
        """声称「已人工确认」却没有确认人 = 无法复核的断言，必须拒绝。"""
        from evaluation.agent_eval.cases import DatasetError, parse_case

        payload = {
            "schema_version": contract.DATASET_SCHEMA_VERSION,
            "case_id": "c1",
            "input": "q",
            "expected_terminal_state": "SUCCEEDED",
            "max_steps": 5,
            "expected_route": "product_info",
            "annotation": {"provenance": "human_confirmed"},
        }
        with pytest.raises(DatasetError, match="confirmed_by"):
            parse_case(payload, 1)

    def test_expected_tools_without_script_is_rejected(self):
        """声明了期望工具却没有脚本化调用 => 工具指标分母会被悄悄掏空。"""
        from evaluation.agent_eval.cases import DatasetError, parse_case

        payload = {
            "schema_version": contract.DATASET_SCHEMA_VERSION,
            "case_id": "c1",
            "input": "q",
            "expected_terminal_state": "SUCCEEDED",
            "max_steps": 5,
            "expected_tools": ["staging_refund"],
        }
        with pytest.raises(DatasetError, match="scripted_tool_calls"):
            parse_case(payload, 1)

    def test_llm_candidate_is_excluded_from_route_denominator(self):
        """LLM 起草的标签不得进入正式路由准确率分母 —— 那就是自我验证。"""
        from evaluation.agent_eval.cases import parse_case

        payload = {
            "schema_version": contract.DATASET_SCHEMA_VERSION,
            "case_id": "c1",
            "input": "q",
            "expected_terminal_state": "SUCCEEDED",
            "max_steps": 5,
            "expected_route": "product_info",
            "annotation": {"provenance": "llm_candidate"},
        }
        assert parse_case(payload, 1).scores_route() is False

    def test_human_confirmed_enters_route_denominator(self):
        from evaluation.agent_eval.cases import parse_case

        payload = {
            "schema_version": contract.DATASET_SCHEMA_VERSION,
            "case_id": "c1",
            "input": "q",
            "expected_terminal_state": "SUCCEEDED",
            "max_steps": 5,
            "expected_route": "product_info",
            "annotation": {
                "provenance": "human_confirmed",
                "method": "human_reviewed",
                "confirmed_by": "reviewer-a",
            },
        }
        assert parse_case(payload, 1).scores_route() is True


class TestShippedDatasetIsHonest:
    def test_shipped_dataset_loads(self):
        from evaluation.agent_eval.cases import load_dataset

        dataset = load_dataset()
        assert len(dataset) > 0
        assert dataset.sha256

    def test_shipped_dataset_has_no_self_confirmed_labels(self):
        """仓库里**不得**出现自我确认的标签。

        若某天有人把全部 case 直接标成 human_confirmed，这条会失败并要求
        说明确认人 —— 那是刻意的：确认必须来自真实的外部评审，不能由写
        数据集的人自己盖章。
        """
        from evaluation.agent_eval.cases import load_dataset

        dataset = load_dataset()
        confirmed = [c for c in dataset.cases if c.annotation.is_human_confirmed]
        assert not confirmed, (
            f"数据集里有 {len(confirmed)} 条自称 human_confirmed 的 case。"
            " 请用 scripts/approve_agent_eval_annotations.py 走人工确认流程，"
            "并填写 confirmed_by。"
        )

    def test_every_case_declares_its_annotation_provenance(self):
        from evaluation.agent_eval.cases import load_dataset

        for case in load_dataset().cases:
            assert case.annotation.provenance in ("human_confirmed", "llm_candidate")
            assert case.annotation.method
