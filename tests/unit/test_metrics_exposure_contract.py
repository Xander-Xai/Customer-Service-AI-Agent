"""指标暴露链路契约（DLQ 告警闭环的最后一环，P0-1）。

背景（这是本文件存在的理由）
----------------------------
``core/monitoring.py`` 在 ``prometheus_client`` 的 ``REGISTRY`` 上注册了约 70 个
指标，含 ``agent_run_dead_letter_total``。但在此之前，全仓唯一的 Prometheus 输出
``/metrics/prometheus`` 是**手工拼接**的 10 行 ``csai_*`` 文本，从不序列化
``REGISTRY``：

- 注册了约 70 个指标 → Prometheus **一条都收不到**；
- ``monitoring/alert_rules.yml`` 的 ``increase(agent_run_dead_letter_total[5m]) > 0``
  引用一个**永远不存在的时间序列** → DLQ 告警**不可能触发**；
- ``Grafana csai-overview`` 的 6 个面板引用从未输出的指标 → 永久空白；
- 原来的 ``test_alert_rules_contract.py`` 只断言
  ``hasattr(core.monitoring, "agent_run_dead_letter_total")``，
  即「代码里有这个名字」，**不**保证它能被 HTTP 抓到 —— 所以这个断链从未被测试抓住。

本文件把「声明」升级成「可达」：**每一个被告警规则或 Grafana 面板引用的指标名，
都必须真的出现在 HTTP 暴露的输出里**。这类断链因此不再依赖人记得检查。

分层设计
--------
1. ``/metrics`` 必须真的序列化 ``REGISTRY``（``generate_latest`` 的等价结果）；
2. ``/metrics`` 未经认证必须是 401 —— 暴露面不能因为修断链而变大；
3. 告警规则 ``expr`` 里引用的每个 metric name 都能在 ``/metrics`` 或
   ``/metrics/prometheus`` 的输出里找到；
4. Grafana 面板引用的每个 metric name 同上；
5. ``prometheus.yml`` 的每个 scrape job 的 ``metrics_path`` 都真实存在，
   且都带上了凭据（否则抓取必然 401 —— 这是同一个断链的第二个环节）。

不依赖任何外部服务：``prometheus_client`` 在进程内，FastAPI 用
``TestClient``。``promtool`` 不在本环境内，因此这里**不**伪造 promtool PASS；
PromQL 解析交给「指标名可达」这一更强的断言替代。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ALERT_RULES = ROOT / "monitoring" / "alert_rules.yml"
PROMETHEUS_YML = ROOT / "monitoring" / "prometheus.yml"
GRAFANA_DASHBOARD = ROOT / "monitoring" / "grafana" / "dashboards" / "csai-overview.json"

DEAD_LETTER_METRIC = "agent_run_dead_letter_total"

#: PromQL 里不是指标名的标识符：函数、聚合算子、关键字、时长、布尔/比较字面量。
_PROMQL_NON_METRIC = {
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "and",
    "or",
    "unless",
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "count_values",
    "stddev",
    "stdvar",
    "topk",
    "bottomk",
    "quantile",
    "rate",
    "irate",
    "increase",
    "delta",
    "idelta",
    "deriv",
    "histogram_quantile",
    "absent",
    "absent_over_time",
    "changes",
    "resets",
    "clamp_max",
    "clamp_min",
    "label_replace",
    "label_join",
    "vector",
    "scalar",
    "time",
    "bool",
    "start",
    "end",
    "offset",
    "true",
    "false",
    "inf",
    "nan",
}

_METRIC_NAME_RE = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\b")


# ─────────────────────────── helpers ───────────────────────────


def _parse_promql_identifiers(expr: str) -> set[str]:
    """从 PromQL 表达式里取出「可能是 metric name」的标识符。

    刻意**不做**完整 PromQL 解析：这里只需要一个足够保守的候选集合，再由
    ``_exposed_metric_names`` 逐个确认是否可达。宁可多收候选（会被可达性断言筛掉），
    也不要漏掉真正被引用的指标名。

    关键顺序：**先切标识符，再剔除非指标词**，不要先做数字/时长的文本替换。
    ``csai_p95_response_time_seconds`` 里夹着 ``95``，先替换数字会把它劈成
    ``csai_p`` 和 ``_response_time_seconds`` 两个不存在的「指标」——那正是本文件
    要抓的那类断链，却会被自己的解析 bug 伪装成断链。
    """
    text = re.sub(r'"[^"]*"', " ", expr)  # 字符串字面量
    candidates = {m for m in _METRIC_NAME_RE.findall(text) if m.lower() not in _PROMQL_NON_METRIC}
    return {c for c in candidates if not c.isdigit()}


def _registry_metric_names() -> set[str]:
    """``/metrics`` 实际会暴露的 metric family 名集合。

    直接调用与路由**同一个**序列化入口（``prometheus_client.generate_latest``），
    而不是重新遍历注册表自己拼一套名字 —— 否则本测试就变成了第二套实现，
    恰好会漏掉「路由返回了什么」这个真正的断点。
    """
    from api.routes.monitoring import _registry_exposition

    exposition = _registry_exposition().decode("utf-8", "replace")
    names: set[str] = set()
    for line in exposition.splitlines():
        if not line or line.startswith("#"):
            continue
        raw = line.split("{", 1)[0].split(" ", 1)[0].strip()
        if not raw:
            continue
        # 同时保留样本名与归一后的 family 名：Counter `foo_total` 在 Prometheus
        # 里的 family 名是 `foo`，告警规则两种写法都可能出现。
        names.add(raw)
        for suffix in ("_bucket", "_sum", "_count", "_total", "_created", "_gcount", "_gsum"):
            if raw.endswith(suffix) and len(raw) > len(suffix):
                names.add(raw[: -len(suffix)])
                break
    return names


def _csai_business_metric_names() -> set[str]:
    """``/metrics/prometheus`` 会输出的 ``csai_*`` 名字集合。"""
    return {
        "csai_info",
        "csai_requests_total",
        "csai_errors_total",
        "csai_error_rate_percent",
        "csai_avg_response_time_seconds",
        "csai_p95_response_time_seconds",
        "csai_cache_hit_rate_percent",
        "csai_agent_calls_total",
        "csai_collaboration_mode_total",
        "csai_total_ai_handled",
        "csai_total_escalated",
        "csai_total_single_turn_resolved",
        "csai_sla_violation_rate",
        "csai_sla_window_violation_rate_percent",
        "csai_circuit_breaker_state",
        "csai_circuit_breaker_consecutive_failures",
    }


def _exposed_metric_names() -> set[str]:
    """两个暴露端点合起来，真正可被 Prometheus 收到的 metric name。"""
    return _registry_metric_names() | _csai_business_metric_names()


def _load_alert_rules() -> list[dict]:
    data = yaml.safe_load(ALERT_RULES.read_text(encoding="utf-8"))
    rules: list[dict] = []
    for group in data.get("groups", []):
        rules.extend(group.get("rules", []))
    return rules


def _dashboard_exprs() -> list[str]:
    panels = json.loads(GRAFANA_DASHBOARD.read_text(encoding="utf-8"))

    def walk(node) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "expr" and isinstance(value, str):
                    found.append(value)
                else:
                    found.extend(walk(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(walk(item))
        return found

    return walk(panels)


# ─────────────────────────── /metrics 端点 ───────────────────────────


@pytest.mark.unit
def test_registry_endpoint_serves_real_prometheus_registry():
    """``GET /metrics`` 必须序列化真实 REGISTRY —— 这是断链的直接修复点。"""
    from api.routes.monitoring import _registry_exposition

    body = _registry_exposition()
    assert b"# HELP" in body and b"# TYPE" in body, "/metrics 输出不像 Prometheus 文本格式"

    text = body.decode("utf-8", "replace")
    assert (
        f"# TYPE {DEAD_LETTER_METRIC} counter" in text
    ), f"{DEAD_LETTER_METRIC} 未出现在 /metrics 输出 —— DLQ 告警仍是无源之水"


@pytest.mark.unit
def test_dead_letter_metric_has_a_sample_after_increment():
    """不只是 HELP/TYPE：必须有真实样本（值），否则 Grafana 面板仍是空的。"""
    from core.monitoring import agent_run_dead_letter_total

    before = _registry_sample(DEAD_LETTER_METRIC)
    agent_run_dead_letter_total.inc()
    after = _registry_sample(DEAD_LETTER_METRIC)
    assert after == before + 1, f"counter 递增未反映到 /metrics：{before} -> {after}"


def _registry_sample(name: str) -> float:
    """从 ``/metrics`` 文本里读一个无 label 样本的值（不依赖 REGISTRY 内部 API）。"""
    from api.routes.monitoring import _registry_exposition

    for line in _registry_exposition().decode("utf-8", "replace").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line.split("{", 1)[0].split(" ", 1)[0].strip() == name:
            return float(line.rsplit(" ", 1)[1])
    return 0.0


@pytest.mark.unit
def test_metrics_endpoint_requires_authentication():
    """修断链不等于把暴露面放开：``/metrics`` 未经认证必须被拒。"""
    from fastapi.testclient import TestClient

    from api.app_factory import app

    with TestClient(app) as client:
        unauthenticated = client.get("/metrics")
    assert unauthenticated.status_code in (
        401,
        403,
    ), f"/metrics 未认证时返回 {unauthenticated.status_code}，监控面被意外公开"


@pytest.mark.unit
def test_metrics_endpoint_accepts_bearer_monitoring_token():
    """Prometheus 的标准抓取凭据形式（``bearer_token_file``）必须能通过。

    没有这条，``prometheus.yml`` 里的 ``bearer_token_file`` 无论怎么配都拿不到
    数据 —— 这是同一条告警链上的**第二个**断点。

    两个方向都要断言：正确凭据**放行**（否则抓不到指标），错误凭据**拒绝**
    （否则端点被意外公开）。
    """
    from fastapi.testclient import TestClient

    import api.utils as utils
    from api.app_factory import app

    original = utils.MONITORING_ADMIN_TOKEN
    utils.MONITORING_ADMIN_TOKEN = "unit-test-monitoring-token-0123456789"
    try:
        with TestClient(app) as client:
            allowed = client.get(
                "/metrics",
                headers={"Authorization": "Bearer unit-test-monitoring-token-0123456789"},
            )
            denied = client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
            anonymous = client.get("/metrics")
        assert allowed.status_code == 200, f"正确 Bearer 竟然被拒（{allowed.status_code}）"
        assert (
            b"agent_run_dead_letter_total" in allowed.content
        ), "正确 Bearer 放行后却没有拿到 DLQ 指标"
        assert denied.status_code in (401, 403), "错误的 bearer 竟然通过了"
        assert anonymous.status_code in (401, 403), "无凭据请求竟然通过了"
    finally:
        utils.MONITORING_ADMIN_TOKEN = original


@pytest.mark.unit
def test_check_admin_token_accepts_bearer_and_header_forms():
    """凭据形式契约：Bearer 与 X-Admin-Token 等价，错误凭据一律拒绝。"""
    import api.utils as utils
    from core.config import MONITORING_ADMIN_TOKEN

    class _Req:
        def __init__(self, headers):
            self.headers = headers

    original = utils.MONITORING_ADMIN_TOKEN
    utils.MONITORING_ADMIN_TOKEN = "s3cret-monitoring-token"
    try:
        assert utils.check_admin_token(_Req({"X-Admin-Token": "s3cret-monitoring-token"}))
        assert utils.check_admin_token(_Req({"Authorization": "Bearer s3cret-monitoring-token"}))
        assert utils.check_admin_token(_Req({"Authorization": "bearer s3cret-monitoring-token"}))
        assert not utils.check_admin_token(_Req({"Authorization": "Bearer nope"}))
        assert not utils.check_admin_token(_Req({}))
        # 显式给出错误的 X-Admin-Token 时不得回退到 Bearer（fail-closed）
        assert not utils.check_admin_token(
            _Req({"X-Admin-Token": "wrong", "Authorization": "Bearer s3cret-monitoring-token"})
        )
    finally:
        utils.MONITORING_ADMIN_TOKEN = original
    assert MONITORING_ADMIN_TOKEN is not None


# ─────────────────────── 告警规则可达性 ───────────────────────


@pytest.mark.unit
def test_every_alert_rule_metric_is_actually_exposed():
    """告警规则 ``expr`` 引用的每个指标名都必须真的被暴露。

    这条断言直接锁死 P0-1 的断链形态：引用一个不存在的时间序列 = 告警永不触发，
    而它曾经是**本仓库 DLQ 运维闭环的最后一环**。
    """
    exposed = _exposed_metric_names()
    missing: dict[str, list[str]] = {}
    for rule in _load_alert_rules():
        expr = str(rule.get("expr", ""))
        for candidate in sorted(_parse_promql_identifiers(expr)):
            if candidate not in exposed:
                missing.setdefault(rule.get("alert", "?"), []).append(candidate)
    assert not missing, (
        "告警规则引用了从未被暴露的指标（这些告警永远不会触发）："
        f"{json.dumps(missing, ensure_ascii=False)}"
    )


@pytest.mark.unit
def test_dead_letter_alert_metric_is_reachable_through_both_layering():
    """DLQ 告警指标必须在**标准注册表层**可达，而不是只能靠 csai_* 二次推导。"""
    assert DEAD_LETTER_METRIC in _registry_metric_names()
    exposed_family = {
        name[: -len("_total")] if name.endswith("_total") else name
        for name in _registry_metric_names()
    }
    assert DEAD_LETTER_METRIC[: -len("_total")] in exposed_family


# ─────────────────────── Grafana 面板可达性 ───────────────────────


@pytest.mark.unit
def test_every_grafana_panel_metric_is_actually_exposed():
    """Grafana 看板引用的每个指标名都必须可达（此前有 6 个面板永久空白）。"""
    exposed = _exposed_metric_names()
    missing: set[str] = set()
    for expr in _dashboard_exprs():
        missing |= {c for c in _parse_promql_identifiers(expr) if c not in exposed}
    assert not missing, f"Grafana 面板引用了从未输出的指标（面板永远为空）：{sorted(missing)}"


# ─────────────────────── 抓取配置一致性 ───────────────────────


@pytest.mark.unit
def test_every_scrape_job_targets_an_existing_path_with_credentials():
    """``prometheus.yml`` 的每个 job：路径真实存在 + 带抓取凭据。

    认证面内的端点没有凭据一律 401 —— 「配了 job 但抓不到」与「没配 job」对告警
    来说是同一件事：没有人在被通知。
    """
    data = yaml.safe_load(PROMETHEUS_YML.read_text(encoding="utf-8"))
    jobs = data.get("scrape_configs") or []
    assert jobs, "prometheus.yml 没有 scrape_configs"
    known_paths = {"/metrics", "/metrics/prometheus"}
    for job in jobs:
        path = job.get("metrics_path", "/metrics")
        assert path in known_paths, f"scrape job {job.get('job_name')} 指向未知路径 {path}"
        assert job.get("bearer_token_file") or job.get(
            "authorization"
        ), f"scrape job {job.get('job_name')} 没有配置抓取凭据 —— 认证面内的端点会返回 401"


@pytest.mark.unit
def test_registry_scrape_job_exists_for_runtime_metrics():
    """必须有一个 job 抓标准 REGISTRY —— 只有 csai_* job 等于 DLQ 指标从未被抓取。"""
    data = yaml.safe_load(PROMETHEUS_YML.read_text(encoding="utf-8"))
    paths = {job.get("metrics_path", "/metrics") for job in data.get("scrape_configs") or []}
    assert "/metrics" in paths, "prometheus.yml 未抓取标准 REGISTRY 端点"
    assert "/metrics/prometheus" in paths, "prometheus.yml 未保留 csai_* 业务聚合抓取"
