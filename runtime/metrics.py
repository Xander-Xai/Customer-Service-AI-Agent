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
    _inc("agent_run_total", status=status)


def record_retry() -> None:
    _inc("agent_run_retry_total")


def record_dead_letter() -> None:
    _inc("agent_run_dead_letter_total")


def record_dead_letter_replay() -> None:
    _inc("agent_run_dead_letter_replay_total")


def record_lock_contention() -> None:
    _inc("agent_thread_lease_contention_total")


def record_lock_acquire() -> None:
    _inc("agent_thread_lease_acquire_total")


def record_thread_lock_renewed(renewed: bool) -> None:
    _inc("agent_thread_lease_renewed_total", outcome="ok" if renewed else "lost")


def record_worker_heartbeat(renewed: bool) -> None:
    _inc("agent_worker_heartbeat", outcome="ok" if renewed else "lost")


def record_run_failure() -> None:
    _inc("agent_run_failed_total")


def record_checkpoint_error() -> None:
    _inc("checkpoint_errors_total")


def record_checkpoint_recovery(recovered: bool, mode: str = "unknown") -> None:
    """记录一次执行是否从已有 checkpoint 恢复（未从第一个节点重跑）。"""
    _inc("agent_checkpoint_recovery_total", mode="recovered" if recovered else mode)


def record_idempotency_hit() -> None:
    _inc("agent_run_idempotency_hit_total")


def record_tool_idempotency_hit() -> None:
    _inc("agent_tool_idempotency_hit_total")


def record_worker_task(status: str) -> None:
    _inc("agent_worker_task_total", status=status)


def observe_run_duration(seconds: float) -> None:
    _observe("agent_run_duration_seconds", max(0.0, seconds))


def observe_queue_wait(seconds: float) -> None:
    _observe("agent_run_queue_wait_seconds", max(0.0, seconds))


def observe_lock_wait(seconds: float) -> None:
    _observe("agent_thread_lease_wait_seconds", max(0.0, seconds))


def inc_worker_active() -> None:
    _change("agent_worker_active", 1)


def dec_worker_active() -> None:
    _change("agent_worker_active", -1)


def inc_run_active() -> None:
    _change("agent_run_inflight", 1)


def dec_run_active() -> None:
    _change("agent_run_inflight", -1)
