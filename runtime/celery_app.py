"""Celery application（Redis broker）。

重要：Celery result backend 不是业务状态真相源；AgentRun（数据库）才是。
因此默认 ``task_ignore_result=True``，且不强制配置 result backend。

投递语义（**实测结论**：Celery 5.6.3 + kombu Redis，真实 worker 进程）
--------------------------------------------------------------------
下表是对 ``Request.on_failure`` 分支的穷举 + 真实 Redis broker 观测的结果，
不是从默认值推断的：

===========================  ========  ==========  =================================
失败类型                     ACK?      重新投递?   Celery 实际动作 / 实测
===========================  ========  ==========  =================================
正常成功                      ack      —           ``acknowledge()``
**普通 Exception**           reject   **否**      ``reject(requeue=False)`` -> 丢弃
**RetryPublicationError**    reject   **否**      同上（普通异常子类）
``SoftTimeLimitExceeded``    reject   **否**      同上
``TimeLimitExceeded``(硬)    reject   窗口内否    子进程被 SIGKILL，消息滞留
                                                     unacked 等 visibility_timeout
``WorkerLostError``(进程死)  reject   **是**      ``reject_on_worker_lost`` 生效
``Reject(requeue=True)``     reject   **是**      **立即**重投，**无退避**
===========================  ========  ==========  =================================

两个必须记住的结论
----------------
1. **异常逃逸 != 重新投递。** ``task_acks_on_failure_or_timeout=False`` 只保证
   失败时不 ACK，Celery 随即执行 ``reject(requeue=False)``，消息被**丢弃**。
   所以 executor 里「让异常逃逸以触发 broker 重新投递」的前提是错的：run 会停在
   ``RETRYING``/``QUEUED``，而**没有任何消息会再来**。
2. **能重新投递的两条路都没有退避**（worker 崩溃重投、``Reject(requeue=True)``）。
   实测 ``Reject(requeue=True)`` 在 5 秒内被重新投递 80 次——裸 reject 会烧穿
   attempt 预算并制造热循环，因此不能拿它当恢复策略。

所以恢复闭环的承重结构是**数据库里的 AgentRun + 周期 reconciler**
（本模块的 ``beat_schedule`` -> ``runtime.reconcile_stuck_runs``），队列消息只是
唤醒信号，而唤醒信号随时可能丢（broker 重启、消息被清理、进程在 publish 前被杀）。
"at-least-once" 仍然成立，但保证它的是**数据库状态机**，不是 broker。
业务 retry 语义（attempt / next_retry_at / DLQ）由 ``runtime/executor.py`` 与
``runtime/run_service.py`` 承担。
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_shutdown, worker_shutting_down

from core.config import (
    AGENT_RUN_QUEUE,
    AGENT_RUN_RECONCILE_INTERVAL_SECONDS,
    AGENT_RUN_TASK_SOFT_TIME_LIMIT,
    AGENT_RUN_TASK_TIME_LIMIT,
    AGENT_RUN_VISIBILITY_TIMEOUT,
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
    # worker 崩溃（WorkerLostError）时消息被重新投递；这是唯一可靠的 broker 重投路径。
    task_acks_late=True,
    # 失败/超时不 ACK。**注意它不带来重投**（见模块 docstring 的实测表）：Celery
    # 走 reject(requeue=False)，消息被丢弃。保留 False 是为了让「丢弃」显式化且不给
    # 失败任务写 result 状态；真正的恢复靠下面的 beat_schedule，不是靠 broker。
    task_acks_on_failure_or_timeout=False,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    # Redis broker 未 ACK 消息的重新可见时间（需 > 最长任务时间，否则会重复投递）
    broker_transport_options={"visibility_timeout": AGENT_RUN_VISIBILITY_TIMEOUT},
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
    # 恢复闭环：周期扫描滞留 run。**必须由独立的 `celery beat` 进程驱动**
    # （compose 里的 beat service），不能内嵌进 worker —— worker 可以被 --scale
    # 成多副本，内嵌 beat 会让每个副本都调度一次。
    beat_schedule={
        "reconcile-stuck-runs": {
            "task": "runtime.reconcile_stuck_runs",
            "schedule": max(1.0, float(AGENT_RUN_RECONCILE_INTERVAL_SECONDS)),
        },
    },
)


@worker_shutting_down.connect
def _on_worker_shutting_down(sender=None, **kwargs):  # pragma: no cover - 信号
    logger.info("Celery worker 收到 shutdown：停止消费新任务，等待当前任务结束")


@worker_process_shutdown.connect
def _on_worker_process_shutdown(sender=None, **kwargs):  # pragma: no cover - 信号
    """子进程退出时关闭 runtime（checkpoint pool / httpx / Redis / thread lock）。"""
    from .async_support import run_sync, shutdown_worker_loop
    from .bootstrap import shutdown_default_runtime
    from .thread_lock import shutdown_thread_lock_manager

    async def _shutdown() -> None:
        await shutdown_default_runtime()
        await shutdown_thread_lock_manager()

    try:
        run_sync(_shutdown())
    except Exception as e:  # pragma: no cover
        logger.warning("worker runtime 关闭异常: %s", type(e).__name__)
    finally:
        shutdown_worker_loop()
