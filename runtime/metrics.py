"""Run 可靠性 / 可观测性指标（Prometheus，缺失时 no-op）。

指标在 core/monitoring.py 注册；本模块只做安全转发，monitoring 不可用时静默
no-op，绝不影响执行路径。
"""

from __future__ import annotations


def _inc(name: str, **labels) -> None:
    try:
        from core import monitoring

        metric = getattr(monitoring, name, None)
        if metric is None:
            return
        if labels:
            metric.labels(**labels).inc()
        else:
            metric.inc()
    except Exception:
        pass


def _observe(name: str, value: float, **labels) -> None:
    try:
        from core import monitoring

        metric = getattr(monitoring, name, None)
        if metric is None:
            return
        if labels:
            metric.labels(**labels).observe(value)
        else:
            metric.observe(value)
    except Exception:
        pass


def _change(name: str, delta: int) -> None:
    try:
        from core import monitoring

        metric = getattr(monitoring, name, None)
        if metric is None:
            return
        if delta >= 0:
            metric.inc()
        else:
            metric.dec()
    except Exception:
        pass


def record_run_status(status: str) -> None:
    _inc("agent_runs_total", status=status)


def record_retry() -> None:
    _inc("agent_run_retry_total")


def record_dead_letter() -> None:
    _inc("agent_run_dead_letter_total")


def record_lock_contention() -> None:
    _inc("agent_thread_lock_contention_total")


def record_lock_acquire() -> None:
    _inc("agent_thread_lock_acquire_total")


def record_run_failure() -> None:
    _inc("agent_run_failures_total")


def record_checkpoint_error() -> None:
    _inc("checkpoint_errors_total")


def record_idempotency_hit() -> None:
    _inc("idempotency_hit_total")


def record_tool_idempotency_hit() -> None:
    _inc("tool_idempotency_hit_total")


def record_worker_task(status: str) -> None:
    _inc("agent_worker_task_total", status=status)


def observe_run_duration(seconds: float) -> None:
    _observe("agent_run_duration_seconds", max(0.0, seconds))


def observe_queue_wait(seconds: float) -> None:
    _observe("agent_run_queue_wait_seconds", max(0.0, seconds))


def observe_lock_wait(seconds: float) -> None:
    _observe("agent_thread_lock_wait_seconds", max(0.0, seconds))


def inc_worker_active() -> None:
    _change("agent_worker_active", 1)


def dec_worker_active() -> None:
    _change("agent_worker_active", -1)


def inc_run_active() -> None:
    _change("agent_runs_active", 1)


def dec_run_active() -> None:
    _change("agent_runs_active", -1)
