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


# ═══════════════════════════════════════════════════════════════════════════
# 恢复闭环告警：beat 存活 ≠ reconciler 成功
# ═══════════════════════════════════════════════════════════════════════════

STALLED_ALERT = "AgentReconcileStalled"
SCAN_FAILING_ALERT = "AgentReconcileScanFailing"
STALLED_METRIC = "agent_reconcile_last_success_timestamp_seconds"
SCAN_METRIC = "agent_reconcile_scan_total"
RECONCILE_GROUP = "csai_alerts"


class TestReconcileStallAlert:
    """恢复闭环停滞必须可告警，且与 beat 存活正交。"""

    def test_both_reconcile_alerts_exist_with_required_fields(self):
        rules = {r["alert"]: r for r in _load_rules()}
        for name, severity in (
            (STALLED_ALERT, "critical"),
            (SCAN_FAILING_ALERT, "warning"),
        ):
            assert name in rules, f"缺少告警规则 {name}"
            rule = rules[name]
            assert rule["labels"]["severity"] == severity
            assert rule["annotations"]["summary"]
            assert rule["annotations"]["description"], f"{name} 缺少 description"
            # `for` 必须存在：否则一次抖动就报警
            assert rule.get("for"), f"{name} 缺少 for（会对抖动过敏）"

    def test_stalled_alert_uses_the_success_timestamp_not_a_failure_counter(self):
        """用「最近一次成功扫描距今多久」，而不是失败计数。

        失败计数只在**有扫描失败**时才增长；「beat 根本没在调度」时它恒为 0，
        永远不会报警。用时间戳才能同时覆盖「调度停了」与「扫描一直失败」。
        """
        rules = {r["alert"]: r for r in _load_rules()}
        expr = rules[STALLED_ALERT]["expr"]
        assert STALLED_METRIC in expr
        assert (
            SCAN_METRIC not in expr
        ), "停滞告警不得依赖扫描失败计数：那在「beat 完全没调度」时恒为 0"

    def test_stalled_alert_covers_the_absent_case(self):
        """指标从未出现时也必须报警。

        否则刚部署 / 尚未成功扫描过一次的那段窗口是无监控的，而恢复闭环可能从一开始
        就是断的。
        """
        rules = {r["alert"]: r for r in _load_rules()}
        expr = rules[STALLED_ALERT]["expr"]
        assert "absent(" in expr, "缺少 absent() 分支 -> 首次部署窗口无监控"

    def test_scan_failing_alert_uses_increase_so_it_does_not_latch(self):
        rules = {r["alert"]: r for r in _load_rules()}
        expr = rules[SCAN_FAILING_ALERT]["expr"]
        assert (
            "increase(" in expr
        ), "扫描失败告警必须用 increase()：写成 > 0 会在历史一次失败后永久报警"

    def test_stalled_threshold_is_a_multiple_of_the_reconcile_interval(self):
        """阈值必须明显大于扫描间隔，否则正常抖动会误报。"""
        import core.config as cfg

        rules = {r["alert"]: r for r in _load_rules()}
        expr = rules[STALLED_ALERT]["expr"]
        threshold = None
        for tok in expr.replace("\n", " ").split():
            if tok.isdigit() and int(tok) > 60:
                threshold = int(tok)
                break
        assert threshold is not None, "阈值必须写成字面量才能被审计"
        assert threshold >= 6 * cfg.AGENT_RUN_RECONCILE_INTERVAL_SECONDS, (
            f"阈值 {threshold}s 相对扫描间隔 {cfg.AGENT_RUN_RECONCILE_INTERVAL_SECONDS}s "
            "过小，正常抖动会误报"
        )

    def test_no_high_cardinality_labels_on_reconcile_alerts(self):
        for rule in _load_rules():
            if rule["alert"] not in (STALLED_ALERT, SCAN_FAILING_ALERT):
                continue
            labels = set(rule.get("labels", {}))
            assert not (
                labels & HIGH_CARDINALITY_LABELS
            ), f"{rule['alert']} 引入高基数 label: {labels & HIGH_CARDINALITY_LABELS}"


class TestReconcileRuleTestScenarios:
    """规则单测必须覆盖四个必需场景（正常 / 停更 / 指标缺失 / 失败）。"""

    def _cases(self) -> tuple[str, list[dict]]:
        data = yaml.safe_load((ROOT / "monitoring" / "alert_rules_test.yml").read_text("utf-8"))
        assert isinstance(data, dict), "alert_rules_test.yml 顶层必须是 mapping"
        assert data.get("rule_files"), "alert_rules_test.yml 缺少 rule_files"
        return data.get("evaluation_interval", "1m"), data.get("tests", [])

    def test_five_scenarios_are_covered(self):
        _, cases = self._cases()
        stalled = [c for c in cases if _alertname(c) == STALLED_ALERT]
        failing = [c for c in cases if _alertname(c) == SCAN_FAILING_ALERT]
        assert len(stalled) >= 3, "Stalled 至少覆盖：健康不报 / 停更报警 / 指标缺失报警"
        assert len(failing) >= 2, "ScanFailing 至少覆盖：失败报警 / 历史失败不 latching"

    def test_generated_annotations_are_in_sync_with_the_rules(self):
        """测试期望的 annotation 必须与规则文件一致（promtool 逐字匹配）。

        手改任何一侧都会让 promtool 失败；用
        ``make alert-rules-sync`` 从规则文件重新生成。
        """
        rules = {r["alert"]: r for r in _load_rules()}
        _, cases = self._cases()
        for case in cases:
            name = _alertname(case)
            if name not in rules:
                continue
            for check in case.get("alert_rule_test", []):
                if check.get("alertname") != name:
                    continue
                for alert in check.get("exp_alerts") or []:
                    exp = alert.get("exp_annotations") or {}
                    actual = rules[name]["annotations"]
                    for key in ("summary", "runbook_url"):
                        if key in actual:
                            assert exp.get(key) == actual[key], (
                                f"{name}.{key} 与 alert_rules.yml 不一致；"
                                "运行 make alert-rules-sync 重新生成"
                            )
                    if "description" in exp:
                        want = actual["description"]
                        got = exp["description"]
                        if "{{ $value" in want:
                            # 含模板的值由 promtool 渲染，无法逐字比较；只要求前后缀同源
                            head, _, tail = want.partition("{{")
                            assert got.startswith(head) and got.endswith(
                                tail.partition("}}")[2]
                            ), f"{name}.description 与模板前缀/后缀不一致"
                        else:
                            assert got == want, (
                                f"{name}.description 与 alert_rules.yml 不一致；"
                                "运行 make alert-rules-sync 重新生成"
                            )


def _alertname(case: dict) -> str | None:
    for check in case.get("alert_rule_test", []) or []:
        return check.get("alertname")
    return None
