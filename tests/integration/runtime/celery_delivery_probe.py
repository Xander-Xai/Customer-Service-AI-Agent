"""Celery 投递语义探针任务（只由 worker 侧 import；测试进程不执行）。

这些任务不碰业务逻辑，只把**可观测事实**写进真实 Redis：

- ``{key}:executions``  —— 执行次数计数器（红elivery 的判定依据）
- ``{key}:task_ids``   —— 每次执行的 Celery task id（观察是否变化）

为什么需要独立模块
------------------
``tests/integration`` 故意没有 ``__init__.py``（会遮蔽应用的 ``runtime`` 包），
所以跨文件能力一律走 fixture / 显式 ``--include``，不走 import。

**每个任务都复用生产 Celery app 的配置**（``runtime/celery_app.py``），
即 ``task_acks_late=True`` / ``task_acks_on_failure_or_timeout=False`` /
``task_reject_on_worker_lost=True``。探针不修改这些开关——否则测的就不是
生产语义。
"""

from __future__ import annotations

import os

import redis as redis_lib

from runtime.celery_app import celery_app
from runtime.executor import RetryPublicationError

REDIS_URL = os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL") or ""


def _rec(key: str):
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
    client.incr(f"{key}:executions")
    client.rpush(f"{key}:task_ids", celery_app.current_task.request.id or "?")
    client.close()


# --------------------------------------------------------------------------
# 场景 1：正常成功
# --------------------------------------------------------------------------


@celery_app.task(name="probe.success", bind=True)
def probe_success(self, key: str) -> str:  # pragma: no cover - worker 侧
    _rec(key)
    return "ok"


# --------------------------------------------------------------------------
# 场景 2：普通 Exception
# --------------------------------------------------------------------------


@celery_app.task(name="probe.ordinary_exception", bind=True)
def probe_ordinary_exception(self, key: str) -> str:  # pragma: no cover - worker 侧
    _rec(key)
    raise RuntimeError("ordinary failure")


# --------------------------------------------------------------------------
# 场景 3：RetryPublicationError（executor 依赖它逃逸来"触发重投"）
# --------------------------------------------------------------------------


@celery_app.task(name="probe.retry_publication_error", bind=True)
def probe_retry_publication_error(self, key: str) -> str:  # pragma: no cover - worker 侧
    _rec(key)
    raise RetryPublicationError("broker down while publishing retry")


# --------------------------------------------------------------------------
# 场景 4：显式 Reject(requeue=True)
# --------------------------------------------------------------------------


@celery_app.task(name="probe.reject_requeue", bind=True)
def probe_reject_requeue(self, key: str) -> str:  # pragma: no cover - worker 侧
    from celery.exceptions import Reject

    _rec(key)
    raise Reject("explicit requeue", requeue=True)


@celery_app.task(name="probe.reject_no_requeue", bind=True)
def probe_reject_no_requeue(self, key: str) -> str:  # pragma: no cover - worker 侧
    from celery.exceptions import Reject

    _rec(key)
    raise Reject("explicit discard", requeue=False)


# --------------------------------------------------------------------------
# 场景 5：worker 被 SIGKILL（WorkerLostError）
# --------------------------------------------------------------------------


@celery_app.task(name="probe.suicide", bind=True)
def probe_suicide(self, key: str) -> str:  # pragma: no cover - worker 侧
    _rec(key)
    import os as _os
    import signal

    _os.kill(_os.getpid(), signal.SIGKILL)
    return "never"  # pragma: no cover


# --------------------------------------------------------------------------
# 场景 6：硬时间上限（TimeLimitExceeded）
# --------------------------------------------------------------------------


@celery_app.task(name="probe.sleep_forever", bind=True)
def probe_sleep_forever(self, key: str) -> str:  # pragma: no cover - worker 侧
    import time

    _rec(key)
    time.sleep(120)
    return "never"  # pragma: no cover


@celery_app.task(name="probe.sleep_softly", bind=True)
def probe_sleep_softly(self, key: str) -> str:  # pragma: no cover - worker 侧
    import time

    _rec(key)
    time.sleep(120)
    return "never"  # pragma: no cover
