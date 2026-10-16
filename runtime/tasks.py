"""Celery 任务定义。

任务 payload 只有 ``run_id``；worker 从数据库加载完整任务信息。Celery task id
作为 ``task_id`` 传入执行器（队列消息 / Worker 执行 ID）。

另含 reconciler 周期任务：它是恢复闭环的**承重结构**，不是可选的锦上添花。
实测（真实 Redis + Celery 5.6.3）普通异常 / ``RetryPublicationError`` / 软硬时间
上限都落在 ``Request.on_failure`` 的 ``reject(requeue=False)`` 分支——消息被丢弃、
不重投。因此「让异常逃逸以触发 redelivery」不成立，run 会停在 QUEUED/RETRYING
且没有任何消息会再来。只有这个周期任务能把它们捞回来。
"""

from __future__ import annotations

from .async_support import run_sync
from .bootstrap import resolve_runtime_provider
from .celery_app import celery_app
from .executor import execute_run


@celery_app.task(name="runtime.execute_agent_run", bind=True)
def execute_agent_run(self, run_id: str) -> str:  # pragma: no cover - 需 broker
    """执行 AgentRun；返回最终状态字符串。"""
    return run_sync(
        execute_run(
            run_id,
            task_id=getattr(self.request, "id", None),
            runtime_provider=resolve_runtime_provider(),
        )
    )


@celery_app.task(name="runtime.reconcile_stuck_runs")
def reconcile_stuck_runs_task() -> dict[str, object]:  # pragma: no cover - 需 broker
    """Celery Beat 周期入口：捞回滞留的 RETRYING/QUEUED 与 lease 过期 RUNNING。

    幂等：重复投递一个已在执行的 run 不会重复执行——``mark_running`` 在他人持有
    有效 lease 时返回 None。单个 run 的投递失败不影响其余（下一轮再试）。

    刻意**不**回收 ``WAITING_APPROVAL``：等待人工审批的 run 不能自动重发，
    理由见 ``runtime/retry.py::reconcile_stuck_runs``。
    """

    from .repository import AgentRunRepository
    from .retry import reconcile_stuck_runs
    from .run_service import RunService

    async def _once() -> list[str]:
        from db.database import get_db_session

        service = RunService(repository=AgentRunRepository(session_factory=get_db_session))
        return await reconcile_stuck_runs(service)

    dispatched = run_sync(_once())
    return {"dispatched": len(dispatched), "run_ids": dispatched}
