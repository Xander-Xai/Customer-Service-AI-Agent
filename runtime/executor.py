"""AgentRun 执行器（worker 逻辑，与 Celery 解耦以便单测）。

可靠性流程：
    thread lock -> RUNNING(worker lease) -> graph -> SUCCEEDED
    竞争 -> 保持 QUEUED + next_retry_at 延迟重调度（不 busy-loop）
    transient -> FAILED -> QUEUED(attempt+1, 指数退避) -> 重新投递
    permanent -> DEAD（不 retry）
    cancelled -> CANCELLED
    attempts 用尽 -> DEAD

跨进程事件流：worker 创建 RunEventPublisher 并通过 contextvar 注入，
graph 的 stream_callback 事件写入 Redis Stream，供 API SSE 读取。

注意：这是 at-least-once delivery + application-level idempotency，
不是端到端 exactly-once。副作用由 tool side-effect store 去重。
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
from .context import (
    reset_event_publisher,
    reset_run_context,
    set_event_publisher,
    set_run_context,
)
from .errors import (
    CANCELLED,
    ThreadLockBackendError,
    classify_exception,
    is_retryable,
    safe_error_message,
)
from .retry import compute_backoff
from .run_service import RunNotFound, RunService
from .statuses import InvalidRunTransition, RunStatus

logger = get_logger("runtime.executor")

RuntimeProvider = Callable[[], Awaitable[Any]]
Dispatcher = Callable[..., Awaitable[Any]]


def _new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _config():
    from core import config

    return config


async def _publish(publisher: Any, event_type: str, **payload: Any) -> None:
    if publisher is None:
        return
    with contextlib.suppress(Exception):
        await publisher.publish({"type": event_type, **payload})


def _get_resume_decision(run_id: str) -> dict[str, Any] | None:
    """若该 run 有已决策且未消费的审批，返回 resume 决策。"""
    try:
        from core.hitl.approval_service import get_approval_service

        return get_approval_service().get_resume_decision(run_id)
    except Exception as e:  # pragma: no cover - best effort
        logger.debug("读取审批决策失败 run=%s: %s", run_id, type(e).__name__)
        return None


def _mark_approval_resumed(approval_id: Any) -> None:
    if not approval_id:
        return
    with contextlib.suppress(Exception):
        from core.hitl.approval_service import get_approval_service

        get_approval_service().mark_resumed(str(approval_id))


async def execute_run(
    run_id: str,
    *,
    service: RunService | None = None,
    runtime_provider: RuntimeProvider | None = None,
    dispatcher: Dispatcher | None = None,
    lock_manager: Any = None,
    worker_id: str | None = None,
    publisher: Any = None,
) -> str:
    """执行一个 AgentRun，返回最终状态字符串。"""
    svc = service or RunService()

    try:
        run = svc.require_run(run_id)
    except RunNotFound:
        logger.warning("execute_run: run 不存在 run_id=%s", run_id)
        return "MISSING"

    status = run["status"]
    if status in (
        RunStatus.SUCCEEDED.value,
        RunStatus.CANCELLED.value,
        RunStatus.DEAD.value,
    ):
        return status  # 幂等：重复投递/取消后投递
    if status == RunStatus.FAILED.value:
        return status  # FAILED 需显式 retry 重新入队

    # 关联 HTTP trace_id -> worker task -> graph（不记录 query，避免高基数/隐私）
    if run.get("trace_id"):
        set_trace_id(run["trace_id"])

    cfg = _config()
    owner = worker_id or _new_worker_id()
    thread_id = run["thread_id"]
    lock_enabled = getattr(cfg, "AGENT_RUN_THREAD_LOCK_ENABLED", True)
    metrics.record_worker_task()

    if publisher is None:
        from .event_publisher import create_run_event_publisher

        publisher = create_run_event_publisher(run_id, thread_id)
    pub_token = set_event_publisher(publisher)

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
                    publisher,
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
                    publisher,
                    reason="thread_locked",
                )

        try:
            try:
                running = svc.mark_running(
                    run_id, worker_id=owner, lease_seconds=cfg.AGENT_RUN_LEASE_SECONDS
                )
            except InvalidRunTransition:
                latest = svc.get_run(run_id)
                return latest["status"] if latest else "MISSING"

            if running is None:
                # 其他 worker 持有有效 lease：重复投递，不重复执行
                logger.info("run=%s 已由其他 worker 持有 lease，跳过", run_id)
                return RunStatus.RUNNING.value

            exec_started = time.perf_counter()
            metrics.inc_worker_active()
            started_at = running.get("started_at")
            queued_at = run.get("queued_at")
            if started_at is not None and queued_at is not None:
                metrics.observe_queue_wait((started_at - queued_at).total_seconds())

            await _publish(publisher, "run_started", attempt=run.get("attempt", 0))

            heartbeat_task = asyncio.create_task(
                _heartbeat_loop(
                    svc,
                    run_id,
                    owner,
                    cfg.AGENT_RUN_LEASE_SECONDS,
                    cfg.AGENT_RUN_HEARTBEAT_SECONDS,
                )
            )
            tokens = set_run_context(run_id, thread_id)
            approval_decision = _get_resume_decision(run_id)
            try:
                if runtime_provider is None:
                    from .bootstrap import get_default_runtime

                    runtime_provider = get_default_runtime
                runtime = await runtime_provider()
                if approval_decision is not None:
                    result = await runtime.resume(
                        thread_id=thread_id, decision=approval_decision
                    )
                    _mark_approval_resumed(approval_decision.get("approval_id"))
                else:
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
                if error_type == CANCELLED:
                    with contextlib.suppress(InvalidRunTransition):
                        svc.mark_cancelled(run_id, reason=error_message)
                    metrics.record_run_status(RunStatus.CANCELLED.value)
                    await _publish(publisher, "run_cancelled", reason=error_message)
                    return RunStatus.CANCELLED.value
                with contextlib.suppress(InvalidRunTransition):
                    svc.mark_failed(
                        run_id,
                        error_code=error_code,
                        error_message=error_message,
                        error_type=error_type,
                    )
                return await _handle_failure(
                    svc,
                    run_id,
                    error_type=error_type,
                    error_code=error_code,
                    error_message=error_message,
                    dispatcher=dispatcher,
                    cfg=cfg,
                    publisher=publisher,
                )
            else:
                if isinstance(result, dict) and result.get("__interrupt__"):
                    # HITL：高风险操作触发人工审批，Graph 已被 checkpoint 暂停
                    svc.mark_waiting_approval(run_id)
                    metrics.record_run_status(RunStatus.WAITING_APPROVAL.value)
                    logger.info("AgentRun 等待人工审批 run_id=%s", run_id)
                    await _publish(
                        publisher,
                        "run_waiting_approval",
                        status=RunStatus.WAITING_APPROVAL.value,
                    )
                    return RunStatus.WAITING_APPROVAL.value
                svc.mark_succeeded(run_id, result)
                metrics.record_run_status(RunStatus.SUCCEEDED.value)
                logger.info("AgentRun 执行成功 run_id=%s", run_id)
                await _publish(
                    publisher,
                    "run_completed",
                    status=RunStatus.SUCCEEDED.value,
                    content=(result or {}).get("response", ""),
                    agent=(result or {}).get("current_agent", ""),
                    mode=(result or {}).get("collaboration_mode", ""),
                )
                return RunStatus.SUCCEEDED.value
            finally:
                reset_run_context(tokens)
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat_task
                metrics.observe_run_duration(time.perf_counter() - exec_started)
                metrics.dec_worker_active()
        finally:
            if lock is not None:
                with contextlib.suppress(ThreadLockBackendError):
                    await lock.release(thread_id, owner)
    finally:
        reset_event_publisher(pub_token)
        with contextlib.suppress(Exception):
            await publisher.close()


async def _handle_failure(
    svc: RunService,
    run_id: str,
    *,
    error_type: str,
    error_code: str,
    error_message: str,
    dispatcher: Dispatcher | None,
    cfg: Any,
    publisher: Any = None,
) -> str:
    run = svc.get_run(run_id)
    if run is None:
        return "MISSING"
    if run["status"] != RunStatus.FAILED.value:
        # 并发取消/终态变化：不再按失败处理
        return run["status"]
    attempt = int(run["attempt"])
    max_attempts = int(run["max_attempts"])

    if not is_retryable(error_type):
        with contextlib.suppress(InvalidRunTransition):
            svc.mark_dead(
                run_id,
                error_code=error_code,
                error_message=error_message,
                error_type=error_type,
            )
        metrics.record_dead()
        metrics.record_run_status(RunStatus.DEAD.value)
        await _publish(
            publisher,
            "run_failed",
            status=RunStatus.DEAD.value,
            error_type=error_type,
            error_code=error_code,
            error_message=error_message,
        )
        return RunStatus.DEAD.value

    if max_attempts <= 1:
        # 未配置重试：FAILED 即终态
        metrics.record_run_status(RunStatus.FAILED.value)
        await _publish(
            publisher,
            "run_failed",
            status=RunStatus.FAILED.value,
            error_type=error_type,
            error_code=error_code,
            error_message=error_message,
        )
        return RunStatus.FAILED.value

    if attempt + 1 < max_attempts:
        delay = compute_backoff(
            attempt,
            base_delay=cfg.AGENT_RUN_RETRY_BASE_DELAY,
            max_delay=cfg.AGENT_RUN_RETRY_MAX_DELAY,
            jitter=cfg.AGENT_RUN_RETRY_JITTER,
        )
        try:
            svc.schedule_retry(
                run_id,
                delay_seconds=delay,
                error_type=error_type,
                error_message=error_message,
            )
        except InvalidRunTransition:
            latest = svc.get_run(run_id)
            return latest["status"] if latest else RunStatus.FAILED.value
        metrics.record_retry()
        metrics.record_run_status(RunStatus.QUEUED.value)
        await _publish(
            publisher,
            "run_queued",
            reason="retry",
            attempt=attempt + 1,
            next_delay_seconds=delay,
        )
        await _call_dispatcher(dispatcher, run_id, delay)
        return RunStatus.QUEUED.value

    with contextlib.suppress(InvalidRunTransition):
        svc.mark_dead(
            run_id, error_code=error_code, error_message=error_message, error_type=error_type
        )
    metrics.record_dead()
    metrics.record_run_status(RunStatus.DEAD.value)
    await _publish(
        publisher,
        "run_failed",
        status=RunStatus.DEAD.value,
        error_type=error_type,
        error_code=error_code,
        error_message=error_message,
    )
    return RunStatus.DEAD.value


async def _defer(
    svc: RunService,
    run_id: str,
    delay: float,
    dispatcher: Dispatcher | None,
    publisher: Any = None,
    *,
    reason: str = "contention",
) -> str:
    svc.defer_run(run_id, delay_seconds=delay)
    await _publish(publisher, "run_queued", reason=reason, next_delay_seconds=delay)
    await _call_dispatcher(dispatcher, run_id, delay)
    latest = svc.get_run(run_id)
    return latest["status"] if latest else "MISSING"


async def _call_dispatcher(
    dispatcher: Dispatcher | None, run_id: str, countdown: float
) -> None:
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
