"""Run 可靠性 / 可观测性指标（Prometheus，缺失时 no-op）。

指标在 core/monitoring.py 注册：
  agent_run_total{status} / agent_run_retry_total / agent_run_dead_total /
  agent_run_duration_seconds / agent_run_queue_wait_seconds / agent_worker_active /
  agent_worker_task_total / thread_lock_contention_total / thread_lock_wait_seconds /
  idempotency_hit_total / tool_idempotency_hit_total / checkpoint_operation_seconds /
  run_event_publish_total / run_event_publish_error_total / sse_connections /
  sse_reconnect_total / run_event_lag_seconds
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


def record_run_status(status: str) -> None:
    _inc("agent_run_total", status=status)


def record_retry() -> None:
    _inc("agent_run_retry_total")


def record_dead() -> None:
    _inc("agent_run_dead_total")


def record_lock_contention() -> None:
    _inc("thread_lock_contention_total")


def record_idempotency_hit() -> None:
    _inc("idempotency_hit_total")


def record_publish() -> None:
    _inc("run_event_publish_total")


def record_publish_error() -> None:
    _inc("run_event_publish_error_total")


def record_sse_reconnect() -> None:
    _inc("sse_reconnect_total")


def record_worker_task() -> None:
    _inc("agent_worker_task_total")


def observe_run_duration(seconds: float) -> None:
    _observe("agent_run_duration_seconds", max(0.0, seconds))


def observe_queue_wait(seconds: float) -> None:
    _observe("agent_run_queue_wait_seconds", max(0.0, seconds))


def observe_lock_wait(seconds: float) -> None:
    _observe("thread_lock_wait_seconds", max(0.0, seconds))


def observe_checkpoint_operation(seconds: float) -> None:
    _observe("checkpoint_operation_seconds", max(0.0, seconds))


def _set(name: str, value: float) -> None:
    try:
        from core import monitoring

        metric = getattr(monitoring, name, None)
        if metric is not None:
            metric.set(value)
    except Exception:
        pass


def _observe(name: str, value: float) -> None:
    try:
        from core import monitoring

        metric = getattr(monitoring, name, None)
        if metric is not None:
            metric.observe(value)
    except Exception:
        pass


def set_sse_connections(value: int) -> None:
    _set("sse_connections", value)


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


def inc_sse_connections() -> None:
    _change("sse_connections", 1)


def dec_sse_connections() -> None:
    _change("sse_connections", -1)


def inc_worker_active() -> None:
    _change("agent_worker_active", 1)


def dec_worker_active() -> None:
    _change("agent_worker_active", -1)


def observe_event_lag(seconds: float) -> None:
    _observe("run_event_lag_seconds", max(0.0, seconds))
