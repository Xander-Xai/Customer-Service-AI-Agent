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


def record_retry_publication_failure() -> None:
    _inc("agent_run_retry_publication_failure_total")


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


def record_worker_heartbeat_error() -> None:
    """续租调用**抛异常**（区别于 renew=False 的「已被接管」）。

    这是 lease 可能丢失的最早信号：早期实现里它会直接杀死续租循环且不留日志，
    导致 run 永久停在 RUNNING。必须可观测。
    """
    _inc("agent_worker_heartbeat_error_total")


def record_ownership_lost_commit() -> None:
    """完成提交因 ownership/lease 失效被拒（worker 仍活着，但已不是 owner）。

    该 run 的终态由 reconciler 接管收敛；这里只统计发生次数。
    """
    _inc("agent_run_ownership_lost_commit_total")


def record_ownership_lost_execution() -> None:
    """失去 ownership 后**取消了正在执行的图**。

    与 ``record_ownership_lost_commit`` 区分：那个是「提交被 owner CAS 拒绝」
    （执行完了才发现没权提交），这个是「执行到一半发现没权，于是主动停下」。后者
    才是 fencing 生效的证据；前者只说明 CAS 兜住了状态。
    """
    _inc("agent_run_ownership_lost_execution_total")


def record_stale_run_reclaimed() -> None:
    """reconciler 重新投递了 lease 已过期的 RUNNING run。"""
    _inc("agent_run_stale_reclaimed_total")


def record_stale_run_dead_lettered() -> None:
    """reconciler 把预算耗尽的过期 RUNNING run 落入了 DLQ。"""
    _inc("agent_run_stale_dead_letter_total")


def record_reconcile_scan(success: bool) -> None:
    """记录一次 reconciler 扫描的**执行结果**（不是 beat 是否存活）。

    与 beat 健康检查正交：beat 的健康检查只看 schedule 文件 mtime，进程活着
    就一直是绿的；而这里记录扫描本身是否成功执行完。两者都为绿才意味着恢复闭环
    真的在跑（``beat 活着 + 扫描失败`` = 闭环已断但健康检查看不出来）。
    """
    _inc("agent_reconcile_scan_total", outcome="success" if success else "error")


def record_reconcile_success(timestamp_seconds: float) -> None:
    """记录最近一次**成功**扫描的 Unix 时间戳（便于直接算恢复闭环是否停滞）。"""
    try:
        from core import monitoring

        metric = getattr(monitoring, "agent_reconcile_last_success_timestamp_seconds", None)
        if metric is not None:
            metric.set(timestamp_seconds)
    except Exception:
        pass


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


def record_side_effect_terminal_write_rejected() -> None:
    """ledger 的 SUCCEEDED/FAILED 结果提交被 CAS 拒绝。

    典型原因：执行期间 claim 被接管、租约已过期、或该操作已进入终态。外部调用的
    结果因此是**未知**的，不能静默当成成功或失败；调用方必须上抛并保留原始异常，
    同时用这个指标让运维看得见「有一条 FAILED 记录没有写进去」。
    """
    _inc("agent_side_effect_terminal_write_rejected_total")


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


# ---------------------------------------------------------------------------
# Human-in-the-loop（审批治理）
# ---------------------------------------------------------------------------


def record_approval_requested(risk_level: str) -> None:
    _inc("agent_approval_requested_total", risk_level=risk_level)
    _change("agent_approval_pending", 1)


def record_approval_decided(decision: str, wait_seconds: float) -> None:
    _inc("agent_approval_decided_total", decision=decision)
    _change("agent_approval_pending", -1)
    _observe("agent_approval_wait_seconds", max(0.0, wait_seconds))


def record_approval_expired() -> None:
    _inc("agent_approval_expired_total")
    _change("agent_approval_pending", -1)


def record_approval_execution(outcome: str) -> None:
    _inc("agent_approval_execution_total", outcome=outcome)
