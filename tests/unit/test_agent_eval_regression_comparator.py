"""Agent Eval 回归比较器的契约测试。

这个比较器回答工业交付里最常被漏掉的问题：**"这次改动有没有让能力退步？"**
只看"当前是否仍高于阈值"的门禁答不了 —— 0.96 掉到 0.94 仍在 0.95 之上，
静默退化会被放行。

本文件锁住三件最容易做错的事：

1. **不可比就说不可比** —— schema 或数据集变了，数字不能相减；
2. **方向来自契约** —— ``lte`` 类指标涨了才是回归，不能一律"跌了才算"；
3. **NOT_MEASURED 不是 0** —— 可测性变化不等于能力退化。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_comparator():
    """按路径加载脚本模块（它不在包内，但仍是受测生产代码）。"""
    path = REPO_ROOT / "scripts" / "compare_agent_eval_regression.py"
    spec = importlib.util.spec_from_file_location("compare_agent_eval_regression", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


comparator = _load_comparator()

pytestmark = pytest.mark.unit


def _artifact(
    *,
    schema: str = "agent-eval-evidence/v1.3",
    dataset_sha: str = "abc123",
    metrics: dict | None = None,
    gate_specs: dict | None = None,
    overall_status: str = "INCONCLUSIVE",
) -> dict:
    metric_entries = {}
    for name, value in (metrics or {}).items():
        if value is None:
            metric_entries[name] = {
                "name": name,
                "status": "NOT_MEASURED",
                "value": None,
                "numerator": 0.0,
                "denominator": 0,
                "excluded": 0,
            }
        else:
            metric_entries[name] = {
                "name": name,
                "status": "MEASURED",
                "value": value,
                "numerator": 1.0,
                "denominator": 2,
                "excluded": 0,
            }
    return {
        "schema_version": schema,
        "overall_status": overall_status,
        "git_sha": "deadbeef",
        "dataset": {"sha256": dataset_sha},
        "code_provenance": {"publication_status": "REPRODUCIBLE", "dirty": False},
        "metrics": metric_entries,
        "gates": {},
        "gate_specs": gate_specs
        if gate_specs is not None
        else {
            "task_completion_rate": {"op": "gte", "threshold": 0.95},
            "safety_violation_rate": {"op": "lte", "threshold": 0.0},
            "tool_execution_success_rate": {"op": "gte", "threshold": 1.0},
        },
    }


class TestUnchangedBaseline:
    def test_identical_artifacts_are_unchanged(self):
        artifact = _artifact(metrics={"task_completion_rate": 0.96})
        report = comparator.compare_artifacts(artifact, artifact)
        assert report["overall"] == comparator.UNCHANGED
        assert report["regressed_metrics"] == []


class TestRegressionDetection:
    def test_gte_metric_dropping_is_a_regression(self):
        baseline = _artifact(metrics={"task_completion_rate": 0.96})
        current = _artifact(metrics={"task_completion_rate": 0.94})
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.REGRESSION
        assert report["regressed_metrics"] == ["task_completion_rate"]
        change = next(c for c in report["changes"] if c["metric"] == "task_completion_rate")
        assert change["verdict"] == comparator.REGRESSION
        assert change["delta"] == pytest.approx(-0.02)

    def test_lte_metric_rising_is_a_regression(self):
        """``safety_violation_rate`` 是 lte（越低越好）—— 涨了才是回归。"""
        baseline = _artifact(metrics={"safety_violation_rate": 0.0})
        current = _artifact(metrics={"safety_violation_rate": 0.05})
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.REGRESSION
        assert report["regressed_metrics"] == ["safety_violation_rate"]

    def test_lte_metric_falling_is_an_improvement(self):
        baseline = _artifact(metrics={"safety_violation_rate": 0.05})
        current = _artifact(metrics={"safety_violation_rate": 0.0})
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.IMPROVEMENT
        assert report["improved_metrics"] == ["safety_violation_rate"]

    def test_gte_metric_rising_is_an_improvement(self):
        baseline = _artifact(metrics={"task_completion_rate": 0.90})
        current = _artifact(metrics={"task_completion_rate": 0.99})
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.IMPROVEMENT

    def test_epsilon_absorbs_float_noise(self):
        baseline = _artifact(metrics={"task_completion_rate": 1.0})
        current = _artifact(metrics={"task_completion_rate": 1.0 - 1e-12})
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.UNCHANGED


class TestIncomparability:
    def test_different_schema_is_incomparable(self):
        """口径变了 → 数字不可比，不能给出能力结论。"""
        baseline = _artifact(
            schema="agent-eval-evidence/v1.2", metrics={"task_completion_rate": 0.96}
        )
        current = _artifact(
            schema="agent-eval-evidence/v1.3", metrics={"task_completion_rate": 0.80}
        )
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.INCOMPARABLE
        assert report["regressed_metrics"] == []
        assert any("schema" in reason for reason in report["incomparability"])

    def test_different_dataset_is_incomparable(self):
        """分母变了 → 百分比不可比。"""
        baseline = _artifact(dataset_sha="aaa", metrics={"task_completion_rate": 0.96})
        current = _artifact(dataset_sha="bbb", metrics={"task_completion_rate": 0.80})
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.INCOMPARABLE
        assert any("dataset" in reason for reason in report["incomparability"])

    @pytest.mark.parametrize(
        ("baseline_sha", "current_sha"),
        [(None, "abc123"), ("abc123", None), (None, None), ("", "abc123")],
    )
    def test_missing_dataset_identity_is_incomparable(self, baseline_sha, current_sha):
        """Unknown denominators cannot be treated as matching dataset identity."""
        baseline = _artifact(dataset_sha=baseline_sha, metrics={"task_completion_rate": 0.96})
        current = _artifact(dataset_sha=current_sha, metrics={"task_completion_rate": 0.80})

        report = comparator.compare_artifacts(baseline, current)

        assert report["overall"] == comparator.INCOMPARABLE
        assert report["regressed_metrics"] == []
        assert any("dataset" in reason for reason in report["incomparability"])

    def test_incomparable_is_not_reported_as_regression(self):
        """不可比时既不能说"退步"，也不能说"没退步"。"""
        baseline = _artifact(dataset_sha="aaa", metrics={"task_completion_rate": 0.96})
        current = _artifact(dataset_sha="bbb", metrics={"task_completion_rate": 0.10})
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.INCOMPARABLE
        assert report["regressed_metrics"] == []

    @pytest.mark.parametrize(
        "publication_status",
        ["DIRTY_WORKTREE_NOT_REPRODUCIBLE", "UNPUBLISHABLE_GENERATOR_MUTATED_SOURCES", None],
    )
    def test_non_reproducible_artifacts_are_incomparable(self, publication_status):
        """A measured artifact from a dirty/unavailable source state cannot gate CI."""
        baseline = _artifact(metrics={"task_completion_rate": 0.96})
        current = _artifact(metrics={"task_completion_rate": 0.80})
        baseline["code_provenance"] = {
            "publication_status": publication_status,
            "dirty": publication_status != "REPRODUCIBLE",
        }

        report = comparator.compare_artifacts(baseline, current)

        assert report["overall"] == comparator.INCOMPARABLE
        assert report["regressed_metrics"] == []
        assert any("provenance" in reason for reason in report["incomparability"])


class TestMeasurabilityChanges:
    def test_metric_newly_measurable_is_not_a_regression(self):
        """基线里 NOT_MEASURED、当前测出来了 —— 是**可测性变化**，不是退步。"""
        baseline = _artifact(metrics={"route_accuracy": None})
        current = _artifact(metrics={"route_accuracy": 0.9})
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.UNCHANGED
        change = next(c for c in report["changes"] if c["metric"] == "route_accuracy")
        assert "measurability changed" in change["reason"]

    def test_metric_becoming_unmeasurable_is_not_a_regression(self):
        baseline = _artifact(metrics={"route_accuracy": 0.9})
        current = _artifact(metrics={"route_accuracy": None})
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.UNCHANGED

    def test_metric_unmeasurable_in_both_is_skipped(self):
        baseline = _artifact(metrics={"route_accuracy": None})
        current = _artifact(metrics={"route_accuracy": None})
        report = comparator.compare_artifacts(baseline, current)
        change = next(c for c in report["changes"] if c["metric"] == "route_accuracy")
        assert change["verdict"] == comparator.UNCHANGED


class TestUngatedMetrics:
    def test_ungated_metric_change_is_reported_but_not_judged(self):
        """人工介入率是负载信号 —— 它涨了不是"能力退步"。"""
        baseline = _artifact(
            metrics={"human_intervention_rate": 0.05},
            gate_specs={
                "task_completion_rate": {"op": "gte", "threshold": 0.95},
            },
        )
        current = _artifact(
            metrics={"human_intervention_rate": 0.40},
            gate_specs={
                "task_completion_rate": {"op": "gte", "threshold": 0.95},
            },
        )
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.UNCHANGED
        change = next(c for c in report["changes"] if c["metric"] == "human_intervention_rate")
        assert change["gated"] is False
        assert change["delta"] == pytest.approx(0.35)

    def test_metric_absent_from_gate_specs_is_not_judged(self):
        baseline = _artifact(metrics={"some_new_metric": 0.5}, gate_specs={})
        current = _artifact(metrics={"some_new_metric": 0.9}, gate_specs={})
        report = comparator.compare_artifacts(baseline, current)
        assert report["overall"] == comparator.UNCHANGED


class TestDirectionComesFromArtifactNotLiveCode:
    def test_gate_specs_are_read_from_the_artifact(self):
        """方向必须来自 artifact 自带的契约，而不是当前代码。"""
        baseline = _artifact(
            metrics={"task_completion_rate": 0.96},
            gate_specs={"task_completion_rate": {"op": "gte", "threshold": 0.95}},
        )
        current = _artifact(
            metrics={"task_completion_rate": 0.90},
            gate_specs={"task_completion_rate": {"op": "gte", "threshold": 0.95}},
        )
        report = comparator.compare_artifacts(baseline, current)
        assert report["changes"][0]["direction"] == "up"
        assert report["overall"] == comparator.REGRESSION

    def test_legacy_artifact_without_gate_specs_does_not_crash(self):
        baseline = _artifact(metrics={"task_completion_rate": 0.96})
        current = _artifact(metrics={"task_completion_rate": 0.90})
        baseline.pop("gate_specs")
        current.pop("gate_specs")
        report = comparator.compare_artifacts(baseline, current)
        # 无方向 → 只报数值变化，不判定回归。
        assert report["overall"] == comparator.UNCHANGED
        assert report["regressed_metrics"] == []


class TestReportShape:
    def test_report_declares_its_schema_and_provenance(self):
        artifact = _artifact(metrics={"task_completion_rate": 0.96})
        report = comparator.compare_artifacts(artifact, artifact)
        assert report["schema_version"] == "agent-eval-regression/v1"
        assert report["baseline"]["dataset_sha256"] == "abc123"
        assert report["current"]["dataset_sha256"] == "abc123"

    def test_notes_state_the_non_blending_rule(self):
        artifact = _artifact(metrics={"task_completion_rate": 0.96})
        report = comparator.compare_artifacts(artifact, artifact)
        assert any("never be compared" in note for note in report["notes"])


class TestCliIncomparabilityExit:
    def test_incomparable_artifacts_fail_the_ci_gate(self, tmp_path):
        """CI must not pass when it cannot establish a meaningful comparison."""
        import json
        import subprocess
        import sys

        dirty = _artifact(metrics={"task_completion_rate": 0.96})
        dirty["code_provenance"] = {
            "publication_status": "DIRTY_WORKTREE_NOT_REPRODUCIBLE",
            "dirty": True,
        }
        current = _artifact(metrics={"task_completion_rate": 0.96})
        baseline_path = tmp_path / "baseline.json"
        current_path = tmp_path / "current.json"
        baseline_path.write_text(json.dumps(dirty), encoding="utf-8")
        current_path.write_text(json.dumps(current), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "compare_agent_eval_regression.py"),
                "--baseline",
                str(baseline_path),
                "--current",
                str(current_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 2
        assert "INCOMPARABLE" in result.stdout
