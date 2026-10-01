"""Celery application（Redis broker）。

重要：Celery result backend 不是业务状态真相源；AgentRun（PostgreSQL）才是。
因此默认 ``task_ignore_result=True``，且不强制配置 result backend。
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_shutdown, worker_shutting_down

from core.config import (
    AGENT_RUN_QUEUE,
    AGENT_RUN_TASK_SOFT_TIME_LIMIT,
    AGENT_RUN_TASK_TIME_LIMIT,
    CELERY_BROKER_URL,
    CELERY_RESULT_BACKEND,
)
from core.logger import get_logger

logger = get_logger("runtime.celery")

celery_app = Celery("csai_agent", broker=CELERY_BROKER_URL)
if CELERY_RESULT_BACKEND:
    celery_app.conf.result_backend = CELERY_RESULT_BACKEND

celery_app.conf.update(
    # 结果不落 result backend（真相源是 agent_runs 表）
    task_ignore_result=True,
    task_default_queue=AGENT_RUN_QUEUE,
    # 任务完成后才 ack；worker 崩溃则任务可重新投递
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    # 只接受 JSON，且 payload 只有 run_id
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_soft_time_limit=AGENT_RUN_TASK_SOFT_TIME_LIMIT,
    task_time_limit=AGENT_RUN_TASK_TIME_LIMIT,
    task_publish_retry=True,
    # 确保任务模块被 worker 加载
    imports=("runtime.tasks",),
)


@worker_shutting_down.connect
def _on_worker_shutting_down(sender=None, **kwargs):  # pragma: no cover - 信号
    logger.info("Celery worker 收到 shutdown：停止消费新任务，等待当前任务结束")


@worker_process_shutdown.connect
def _on_worker_process_shutdown(sender=None, **kwargs):  # pragma: no cover - 信号
    """子进程退出时关闭 runtime（checkpoint pool / httpx / Redis / thread lock）。"""
    from .async_support import run_sync, shutdown_worker_loop
    from .bootstrap import shutdown_default_runtime
    from .event_stream import shutdown_run_event_stream
    from .thread_lock import shutdown_thread_lock_manager

    async def _shutdown() -> None:
        await shutdown_default_runtime()
        await shutdown_thread_lock_manager()
        await shutdown_run_event_stream()

    try:
        run_sync(_shutdown())
    except Exception as e:  # pragma: no cover
        logger.warning("worker runtime 关闭异常: %s", type(e).__name__)
    finally:
        shutdown_worker_loop()
