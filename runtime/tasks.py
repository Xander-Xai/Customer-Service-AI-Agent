"""Celery 任务定义。

任务 payload 只有 ``run_id``；worker 从数据库加载完整任务信息。Celery task id
作为 ``task_id`` 传入执行器（队列消息 / Worker 执行 ID）。
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
