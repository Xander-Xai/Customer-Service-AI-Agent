"""Gate 14：Runtime Prometheus 指标契约（无外部依赖，进程内断言）。

两件事：
  1. 规范要求的指标**必须存在**于 Prometheus registry；
  2. 任何 ``agent_*`` 指标的 label 都**不得**包含高基数字段（run_id / thread_id /
     user_id / query / session_id）。这些字段每请求唯一，会让时序数无界增长，
     把 Prometheus 打死。label 只允许 status / mode / agent / error_type 等低基数维度。
"""

from __future__ import annotations

import pytest

REQUIRED_METRICS = (
    "agent_run_total",
    "agent_run_duration_seconds",
    "agent_run_inflight",
    "agent_run_retry_total",
    "agent_run_failed_total",
    "agent_run_dead_letter_total",
    "agent_thread_lease_acquire_total",
    "agent_thread_lease_contention_total",
    "agent_checkpoint_recovery_total",
    "agent_tool_idempotency_hit_total",
    "agent_worker_heartbeat",
)

FORBIDDEN_LABELS = {
    "run_id",
    "thread_id",
    "user_id",
    "query",
    "customer_query",
    "session_id",
    "idempotency_key",
    "tool_call_id",
    "error_message",
    "error",
}


def _samples() -> list[tuple[str, set[str]]]:
    """registry 中所有**样本名**及其 label 集合。

    注意 prometheus_client 的 ``Counter("agent_run_total")`` 注册的 family 名是
    ``agent_run``，样本名才是 ``agent_run_total``；断言存在性必须看样本名。
    """
    from prometheus_client import REGISTRY

    out: list[tuple[str, set[str]]] = []
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            out.append((sample.name, set(sample.labels.keys())))
    return out


def _warm_up() -> None:
    """先调用一次转发器，让带 label 的 Counter/Gauge 产生首个样本。

    Prometheus 对「带 label 但从未自增」的指标不导出任何样本，因此不预热就无法用
    registry 断言它们的存在。
    """
    from runtime import metrics

    metrics.record_run_status("SUCCEEDED")
    metrics.record_retry()
    metrics.record_dead_letter()
    metrics.record_dead_letter_replay()
    metrics.record_run_failure()
    metrics.record_lock_acquire()
    metrics.record_lock_contention()
    metrics.record_thread_lock_renewed(True)
    metrics.record_worker_heartbeat(True)
    metrics.record_checkpoint_error()
    metrics.record_checkpoint_recovery(True)
    metrics.record_idempotency_hit()
    metrics.record_tool_idempotency_hit()
    metrics.record_worker_task("started")
    metrics.observe_run_duration(0.1)
    metrics.observe_queue_wait(0.1)
    metrics.observe_lock_wait(0.1)
    metrics.inc_run_active()
    metrics.dec_run_active()
    metrics.inc_worker_active()
    metrics.dec_worker_active()


def _is_registered(name: str, names: set[str]) -> bool:
    # Histogram 会产生 _bucket/_count/_sum 子样本，基名本身不是样本
    return name in names or any(n.startswith(f"{name}_") for n in names)


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_METRICS)
def test_required_agent_metric_exists(name):
    from core import monitoring  # noqa: F401  触发指标注册

    _warm_up()
    names = {s for s, _ in _samples()}
    assert hasattr(monitoring, name), f"core.monitoring 缺少指标 {name}"
    assert _is_registered(name, names), (
        f"{name} 未注册到 Prometheus registry（现有 agent_ 指标: "
        f"{sorted(n for n in names if n.startswith('agent_'))}）"
    )


@pytest.mark.unit
def test_no_high_cardinality_labels_on_agent_metrics():
    """agent_* 指标不得使用 run_id/thread_id/user_id/query 等高基数 label。"""
    violations = []
    for metric_name, labels in _samples():
        if not metric_name.startswith("agent_"):
            continue
        bad = labels & FORBIDDEN_LABELS
        if bad:
            violations.append(f"{metric_name}: {sorted(bad)}")
    assert not violations, "高基数 label 会导致 Prometheus 时序爆炸: " + "; ".join(violations)


@pytest.mark.unit
def test_runtime_metrics_forwarders_exist():
    """runtime.metrics 的转发函数必须与指标名一一对应（避免改名后静默失效）。"""
    from runtime import metrics

    expected = {
        "record_run_status": "agent_run_total",
        "record_retry": "agent_run_retry_total",
        "record_dead_letter": "agent_run_dead_letter_total",
        "record_dead_letter_replay": "agent_run_dead_letter_replay_total",
        "record_run_failure": "agent_run_failed_total",
        "record_lock_acquire": "agent_thread_lease_acquire_total",
        "record_lock_contention": "agent_thread_lease_contention_total",
        "record_thread_lock_renewed": "agent_thread_lease_renewed_total",
        "record_worker_heartbeat": "agent_worker_heartbeat",
        "record_checkpoint_error": "checkpoint_errors_total",
        "record_checkpoint_recovery": "agent_checkpoint_recovery_total",
        "record_idempotency_hit": "agent_run_idempotency_hit_total",
        "record_tool_idempotency_hit": "agent_tool_idempotency_hit_total",
        "observe_run_duration": "agent_run_duration_seconds",
        "observe_queue_wait": "agent_run_queue_wait_seconds",
        "observe_lock_wait": "agent_thread_lease_wait_seconds",
        "inc_run_active": "agent_run_inflight",
        "dec_run_active": "agent_run_inflight",
        "inc_worker_active": "agent_worker_active",
        "dec_worker_active": "agent_worker_active",
        "record_worker_task": "agent_worker_task_total",
    }
    from core import monitoring

    for fn_name, metric_name in expected.items():
        assert hasattr(metrics, fn_name), f"runtime.metrics 缺少转发函数 {fn_name}"
        assert hasattr(
            monitoring, metric_name
        ), f"{fn_name} 指向的指标 {metric_name} 未在 core.monitoring 定义"


@pytest.mark.unit
def test_forwarder_records_into_registry():
    """转发函数必须真的写进 registry（防止 getattr 静默返回 None）。"""
    from prometheus_client import REGISTRY

    from runtime import metrics

    def total(name: str) -> float:
        value = 0.0
        for metric in REGISTRY.collect():
            for sample in metric.samples:
                if sample.name in (name, f"{name}_total"):
                    value += sample.value
        return value

    before = total("agent_thread_lease_renewed_total")
    metrics.record_thread_lock_renewed(True)
    assert total("agent_thread_lease_renewed_total") == before + 1

    before_hb = total("agent_worker_heartbeat")
    metrics.record_worker_heartbeat(False)
    assert total("agent_worker_heartbeat") == before_hb + 1

    before_ck = total("agent_checkpoint_recovery_total")
    metrics.record_checkpoint_recovery(True)
    assert total("agent_checkpoint_recovery_total") == before_ck + 1


@pytest.mark.unit
def test_scripts_dir_is_excluded_from_pytest_collection():
    """``scripts/`` 必须被 pytest 排除，否则同名模块会让整个收集失败。

    回归背景：``scripts/test_worker_crash_recovery.py``（runtime 混沌验收脚本）与
    ``tests/integration/test_worker_crash_recovery.py`` 同名。裸跑 ``pytest`` 时前者被
    当作测试模块导入，后者随即报 "import file mismatch" 并中断整个收集——一次失败
    会让全部测试无法运行。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"(?m)^norecursedirs\s*=\s*\[(.*?)\]", text, re.S)
    assert match, "pyproject.toml 缺少 norecursedirs 配置"
    excluded = re.findall(r'"([^"]+)"', match.group(1))
    assert any(
        d.rstrip("/") == "scripts" for d in excluded
    ), f"scripts/ 必须列入 norecursedirs，当前: {excluded}"
