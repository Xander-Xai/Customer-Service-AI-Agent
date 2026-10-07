"""DLQ 告警规则契约（无外部依赖）。

验证 ``monitoring/alert_rules.yml`` 里的 dead-letter 告警：

- 使用**真实**指标 ``agent_run_dead_letter_total``（与 ``runtime/metrics.py``
  的转发函数和 ``core/monitoring.py`` 的注册一致）；
- 用 ``increase(...[window]) > 0`` 检测**最近新增**，而不是
  ``agent_run_dead_letter_total > 0``（后者历史一次失败后永久报警）；
- 带 ``for`` / ``severity`` / ``summary`` / ``description`` / ``runbook_url``；
- 不引入 run_id / thread_id / task_id / user_id / query 等高基数 label。

``promtool`` 不在本环境内，因此这里只做结构/语义断言，不伪造 promtool PASS。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ALERT_RULES = ROOT / "monitoring" / "alert_rules.yml"

DEAD_LETTER_ALERT = "AgentRunDeadLetterDetected"
DEAD_LETTER_METRIC = "agent_run_dead_letter_total"

HIGH_CARDINALITY_LABELS = {
    "run_id",
    "thread_id",
    "task_id",
    "user_id",
    "session_id",
    "query",
    "idempotency_key",
    "tool_call_id",
    "error_message",
}


def _load_rules() -> list[dict]:
    data = yaml.safe_load(ALERT_RULES.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "alert_rules.yml 顶层必须是 mapping"
    groups = data.get("groups")
    assert isinstance(groups, list) and groups, "alert_rules.yml 缺少 groups"
    rules: list[dict] = []
    for group in groups:
        for rule in group.get("rules", []):
            rules.append(rule)
    return rules


def _dead_letter_rule() -> dict:
    for rule in _load_rules():
        if rule.get("alert") == DEAD_LETTER_ALERT:
            return rule
    raise AssertionError(f"缺少告警规则 {DEAD_LETTER_ALERT}")


@pytest.mark.unit
def test_alert_rules_file_parses():
    rules = _load_rules()
    assert rules, "alert_rules.yml 未解析出任何规则"
    names = {r.get("alert") for r in rules}
    assert DEAD_LETTER_ALERT in names


@pytest.mark.unit
def test_dead_letter_alert_uses_increase_on_real_metric():
    expr = str(_dead_letter_rule().get("expr", ""))
    assert DEAD_LETTER_METRIC in expr, f"expr 未使用真实指标: {expr!r}"
    assert "increase(" in expr, f"expr 必须用 increase() 只看最近新增: {expr!r}"
    assert "> 0" in expr, f"expr 必须是比较阈值: {expr!r}"


@pytest.mark.unit
def test_dead_letter_alert_does_not_latch_on_historical_counter():
    expr = str(_dead_letter_rule().get("expr", ""))
    assert (
        f"{DEAD_LETTER_METRIC} > 0" not in expr
    ), "不得使用裸 counter > 0（首次 dead-letter 后会永久报警）"


@pytest.mark.unit
def test_dead_letter_alert_has_required_annotations_and_severity():
    rule = _dead_letter_rule()
    assert rule.get("for"), "告警必须带 for"
    labels = rule.get("labels") or {}
    assert labels.get("severity") in {"critical", "warning"}, labels
    annotations = rule.get("annotations") or {}
    assert annotations.get("summary"), "缺少 summary"
    assert annotations.get("description"), "缺少 description"
    assert annotations.get("runbook_url"), "缺少 runbook_url"


@pytest.mark.unit
def test_dead_letter_alert_runbook_points_at_dlq_section():
    url = str((_dead_letter_rule().get("annotations") or {}).get("runbook_url", ""))
    assert "distributed-runtime-runbook.md" in url, url
    assert "dead_letter" in url.lower(), url


@pytest.mark.unit
def test_dead_letter_alert_has_no_high_cardinality_labels():
    rule = _dead_letter_rule()
    label_keys = set((rule.get("labels") or {}).keys())
    bad = label_keys & HIGH_CARDINALITY_LABELS
    assert not bad, f"告警引入了高基数 label: {sorted(bad)}"


@pytest.mark.unit
def test_dead_letter_metric_name_matches_runtime_forwarder():
    """告警用的指标名必须与 runtime.metrics 的转发目标一致（改名即失败）。"""
    from runtime import metrics

    source = Path(metrics.__file__).read_text(encoding="utf-8")
    assert DEAD_LETTER_METRIC in source, f"runtime/metrics.py 未引用 {DEAD_LETTER_METRIC}"


@pytest.mark.unit
def test_dead_letter_metric_is_registered_in_monitoring():
    from core import monitoring

    assert hasattr(monitoring, DEAD_LETTER_METRIC), f"core.monitoring 未注册 {DEAD_LETTER_METRIC}"
