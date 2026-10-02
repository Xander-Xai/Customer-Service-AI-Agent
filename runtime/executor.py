"""AgentRun 执行器（worker 逻辑，与 Celery 解耦以便单测）。

可靠性流程::

    thread lock -> RUNNING(worker lease) -> graph -> SUCCEEDED
    竞争          -> 保持 QUEUED/RETRYING + next_retry_at 延迟重调度（不 busy-loop）
    transient     -> RETRYING(attempt+1, 指数退避) -> 重新投递
    retry 用尽    -> DEAD_LETTER（写 application-level DLQ）
    permanent     -> FAILED（不 retry）
    终态重复投递  -> no-op（application-level run 幂等）

注意：这是 at-least-once delivery + application-level idempotency，不是端到端
exactly-once。外部副作用由 tool side-effect ledger 去重（见 side_effects.py）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from core.logger import get_logger, set_trace_id

from . import metrics
from .context import reset_run_context, set_run_context
from .errors import (
    ThreadLockBackendError,
    classify_exception,
    is_retryable,
    safe_error_message,
)
from .retry import compute_backoff
from .run_service import RunNotFound, RunService
from .statuses import TERMINAL_STATUSES, InvalidRunTransition, RunStatus

logger = get_logger("runtime.executor")

RuntimeProvider = Callable[[], Awaitable[Any]]
Dispatcher = Callable[..., Awaitable[Any]]


def _new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _config():
    from core import config

    return config


async def execute_run(
    run_id: str,
    *,
    service: RunService | None = None,
    runtime_provider: RuntimeProvider | None = None,
    dispatcher: Dispatcher | None = None,
    lock_manager: Any = None,
    worker_id: str | None = None,
    task_id: str | None = None,
) -> str:
    """执行一个 AgentRun，返回最终状态字符串。"""
    svc = service or RunService()

    try:
        run = svc.require_run(run_id)
    except RunNotFound:
        logger.warning("execute_run: run 不存在 run_id=%s", run_id)
        return "MISSING"

    status = run["status"]
    if status in {s.value for s in TERMINAL_STATUSES}:
        # application-level 幂等：重复投递终态 run 直接 no-op
        logger.info("execute_run: run=%s 已是终态 %s，跳过", run_id, status)
        return str(status)

    if run.get("trace_id"):
        set_trace_id(run["trace_id"])

    cfg = _config()
    owner = worker_id or _new_worker_id()
    thread_id = run["thread_id"]
    lock_enabled = getattr(cfg, "AGENT_RUN_THREAD_LOCK_ENABLED", True)
    metrics.record_worker_task("started")

    lock = lock_manager
    if lock is None and lock_enabled:
        from .thread_lock import get_thread_lock_manager

        lock = get_thread_lock_manager()

    try:
        if lock is not None:
            lock_started = time.perf_counter()
            try:
                acquired = await lock.acquire(
                    thread_id, owner, cfg.AGENT_RUN_THREAD_LOCK_TTL_SECONDS
                )
            except ThreadLockBackendError as e:
                metrics.observe_lock_wait(time.perf_counter() - lock_started)
                logger.warning("thread lock 后端不可用，延迟重调度 run=%s: %s", run_id, e)
                return await _defer(
                    svc,
                    run_id,
                    cfg.AGENT_RUN_THREAD_LOCK_RETRY_DELAY_SECONDS,
                    dispatcher,
                    reason="lock_backend_unavailable",
                )
            metrics.observe_lock_wait(time.perf_counter() - lock_started)
            if not acquired:
                metrics.record_lock_contention()
                logger.info("thread 竞争，延迟重调度 run=%s thread=%s", run_id, thread_id)
                return await _defer(
                    svc,
                    run_id,
                    cfg.AGENT_RUN_THREAD_LOCK_RETRY_DELAY_SECONDS,
                    dispatcher,
                    reason="thread_locked",
                )

        try:
            try:
                running = svc.mark_running(
                    run_id,
                    worker_id=owner,
                    task_id=task_id,
                    lease_seconds=cfg.AGENT_RUN_LEASE_SECONDS,
                )
            except InvalidRunTransition:
                latest = svc.get_run(run_id)
                return str(latest["status"]) if latest else "MISSING"

            if running is None:
                # 其他 worker 持有有效 lease：重复投递，不重复执行
                logger.info("run=%s 已由其他 worker 持有 lease，跳过", run_id)
                return RunStatus.RUNNING.value

            exec_started = time.perf_counter()
            metrics.inc_worker_active()
            metrics.inc_run_active()
            started_at = running.get("started_at")
            queued_at = run.get("queued_at")
            if started_at is not None and queued_at is not None:
                metrics.observe_queue_wait((started_at - queued_at).total_seconds())

            heartbeat_task = asyncio.create_task(
                _heartbeat_loop(
                    svc,
                    run_id,
                    owner,
                    cfg.AGENT_RUN_LEASE_SECONDS,
                    cfg.AGENT_RUN_HEARTBEAT_SECONDS,
                )
            )
            tokens = set_run_context(run_id, thread_id, task_id)
            try:
                if runtime_provider is None:
                    from .bootstrap import get_default_runtime

                    runtime_provider = get_default_runtime
                runtime = await runtime_provider()
                result = await runtime.run(
                    thread_id=thread_id,
                    query=run["query"],
                    user_id=run.get("user_id"),
                )
            except Exception as e:
                error_type = classify_exception(e)
                error_code = type(e).__name__
                error_message = safe_error_message(e)
                logger.error(
                    "AgentRun 执行失败 run_id=%s type=%s code=%s",
                    run_id,
                    error_type,
                    error_code,
                )
                return await _handle_failure(
                    svc,
                    run_id,
                    error_type=error_type,
                    error_code=error_code,
                    error_message=error_message,
                    dispatcher=dispatcher,
                    cfg=cfg,
                    worker_id=owner,
                )
            else:
                svc.mark_succeeded(run_id, result)
                metrics.record_run_status(RunStatus.SUCCEEDED.value)
                metrics.record_worker_task("succeeded")
                logger.info("AgentRun 执行成功 run_id=%s", run_id)
                return RunStatus.SUCCEEDED.value
            finally:
                reset_run_context(tokens)
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat_task
                metrics.observe_run_duration(time.perf_counter() - exec_started)
                metrics.dec_worker_active()
                metrics.dec_run_active()
        finally:
            if lock is not None:
                with contextlib.suppress(ThreadLockBackendError):
                    await lock.release(thread_id, owner)
    except Exception as e:  # pragma: no cover - 未预期错误：标记失败避免卡死
        logger.error("execute_run 未预期异常 run_id=%s: %s", run_id, type(e).__name__)
        with contextlib.suppress(Exception):
            latest = svc.get_run(run_id)
            if latest is not None and latest["status"] == RunStatus.RUNNING.value:
                svc.mark_failed(
                    run_id,
                    error_code=type(e).__name__,
                    error_message=safe_error_message(e),
                    error_type="permanent",
                )
                metrics.record_worker_task("failed")
        return RunStatus.FAILED.value


async def _handle_failure(
    svc: RunService,
    run_id: str,
    *,
    error_type: str,
    error_code: str,
    error_message: str,
    dispatcher: Dispatcher | None,
    cfg: Any,
    worker_id: str | None = None,
) -> str:
    run = svc.get_run(run_id)
    if run is None:
        return "MISSING"
    if run["status"] != RunStatus.RUNNING.value:
        # 并发变化（终态/接管）：不再按本次失败处理
        return str(run["status"])

    attempt = int(run["attempt"])
    max_attempts = int(run["max_attempts"])

    if not is_retryable(error_type):
        with contextlib.suppress(InvalidRunTransition):
            svc.mark_failed(
                run_id,
                error_code=error_code,
                error_message=error_message,
                error_type=error_type,
            )
        metrics.record_run_status(RunStatus.FAILED.value)
        metrics.record_run_failure()
        metrics.record_worker_task("failed")
        logger.info("AgentRun permanent 失败 run_id=%s attempt=%s", run_id, attempt)
        return RunStatus.FAILED.value

    if attempt >= max_attempts:
        with contextlib.suppress(InvalidRunTransition):
            svc.mark_dead_letter(
                run_id,
                error_code=error_code,
                error_message=error_message,
                error_type=error_type,
                worker_id=worker_id,
            )
        metrics.record_dead_letter()
        metrics.record_run_status(RunStatus.DEAD_LETTER.value)
        metrics.record_run_failure()
        metrics.record_worker_task("failed")
        logger.warning(
            "AgentRun retry 用尽 -> DEAD_LETTER run_id=%s attempt=%s/%s",
            run_id,
            attempt,
            max_attempts,
        )
        return RunStatus.DEAD_LETTER.value

    delay = compute_backoff(
        attempt - 1,
        base_delay=cfg.AGENT_RUN_RETRY_BASE_DELAY,
        max_delay=cfg.AGENT_RUN_RETRY_MAX_DELAY,
        jitter=cfg.AGENT_RUN_RETRY_JITTER,
    )
    try:
        svc.mark_retrying(
            run_id,
            delay_seconds=delay,
            error_type=error_type,
            error_code=error_code,
            error_message=error_message,
        )
    except InvalidRunTransition:
        latest = svc.get_run(run_id)
        return latest["status"] if latest else RunStatus.FAILED.value
    metrics.record_retry()
    metrics.record_run_status(RunStatus.RETRYING.value)
    metrics.record_worker_task("retrying")
    logger.info(
        "AgentRun transient 失败 -> RETRYING run_id=%s attempt=%s/%s delay=%.2fs",
        run_id,
        attempt,
        max_attempts,
        delay,
    )
    await _call_dispatcher(dispatcher, run_id, delay)
    return RunStatus.RETRYING.value


async def _defer(
    svc: RunService,
    run_id: str,
    delay: float,
    dispatcher: Dispatcher | None,
    *,
    reason: str = "contention",
) -> str:
    svc.defer_run(run_id, delay_seconds=delay)
    await _call_dispatcher(dispatcher, run_id, delay)
    latest = svc.get_run(run_id)
    return latest["status"] if latest else "MISSING"


async def _call_dispatcher(dispatcher: Dispatcher | None, run_id: str, countdown: float) -> None:
    if dispatcher is None:
        from .dispatch import dispatch_run

        dispatcher = dispatch_run
    if countdown and countdown > 0:
        try:
            await dispatcher(run_id, countdown=countdown)
            return
        except TypeError:
            pass
    await dispatcher(run_id)


async def _heartbeat_loop(
    svc: RunService, run_id: str, worker_id: str, lease_seconds: float, interval: float
) -> None:
    try:
        while True:
            await asyncio.sleep(max(1.0, interval))
            if not svc.heartbeat(run_id, worker_id=worker_id, lease_seconds=lease_seconds):
                return
    except asyncio.CancelledError:
        return
