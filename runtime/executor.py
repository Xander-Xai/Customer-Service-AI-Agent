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
from .events import (
    EVENT_COMPLETED,
    EVENT_FAILED,
    EVENT_RETRY,
    EVENT_STARTED,
    RunEventPublisher,
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


async def _publish(event: str, run_id: str, svc: RunService, **payload) -> None:
    """把 run 生命周期事件写入 Redis Stream（观测通道，失败不影响业务）。"""
    publisher = getattr(svc, "event_publisher", None)
    if publisher is None:
        publisher = _shared_event_publisher()
    await publisher.publish(run_id, event, payload)


_shared_publisher: RunEventPublisher | None = None


def _shared_event_publisher() -> RunEventPublisher:
    global _shared_publisher
    if _shared_publisher is None:
        _shared_publisher = RunEventPublisher()
    return _shared_publisher


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
            with contextlib.suppress(Exception):
                await _publish(
                    EVENT_STARTED,
                    run_id,
                    svc,
                    thread_id=thread_id,
                    status=RunStatus.RUNNING.value,
                    attempt=running.get("attempt"),
                )
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
                    lock=lock,
                    thread_id=thread_id,
                    lock_ttl_seconds=cfg.AGENT_RUN_THREAD_LOCK_TTL_SECONDS,
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
                with contextlib.suppress(Exception):
                    await _publish(
                        EVENT_COMPLETED,
                        run_id,
                        svc,
                        thread_id=thread_id,
                        status=RunStatus.SUCCEEDED.value,
                        attempt=running.get("attempt"),
                        duration_seconds=round(time.perf_counter() - exec_started, 3),
                    )
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
    except RetryPublicationError:
        # 重试消息投递失败：run 已经在 RETRYING/QUEUED 等一条**不会到来**的消息。
        # 这里绝不能 ACK，也不能把它改成 FAILED（那会丢掉一次合法重试机会）。
        # 逃逸出任务 -> Celery acks_late 不 ACK -> broker 重新投递 -> 租约到期后被接管。
        logger.error(
            "重试投递失败，让任务逃逸以触发 broker redelivery: run_id=%s", run_id
        )
        metrics.record_retry_publication_failure()
        raise
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
            elif latest is not None and latest["status"] in (
                RunStatus.RETRYING.value,
                RunStatus.QUEUED.value,
            ):
                # 同样不能 ACK：状态在等重投/退避，却没有任何调度器在跑。
                logger.error(
                    "run 停在 %s 但无待执行投递，逃逸以触发 redelivery: run_id=%s",
                    latest["status"],
                    run_id,
                )
                metrics.record_retry_publication_failure()
                raise
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
        with contextlib.suppress(Exception):
            await _publish(
                EVENT_FAILED,
                run_id,
                svc,
                attempt=attempt,
                error_code=error_code,
                error_type=error_type,
                reason="permanent",
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
        with contextlib.suppress(Exception):
            await _publish(
                EVENT_FAILED,
                run_id,
                svc,
                attempt=attempt,
                max_attempts=max_attempts,
                error_code=error_code,
                error_type=error_type,
                reason="dead_letter",
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
    with contextlib.suppress(Exception):
        await _publish(
            EVENT_RETRY,
            run_id,
            svc,
            attempt=attempt,
            max_attempts=max_attempts,
            error_code=error_code,
            error_type=error_type,
            reason="transient",
        )
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


class RetryPublicationError(RuntimeError):
    """延迟重试的**投递失败**：run 已经进入 RETRYING/QUEUED，但消息没发出去。

    这是一个**必须让异常逃逸**的错误：只要吞掉它，Celery 就会 ACK 当前任务，而队列里
    又没有替代消息，run 会永远停在 RETRYING（既不会重跑，也不会失败）。让异常逃出
    任务，Celery 的 ``acks_late`` 就会把它重新投递，由租约到期后的接管者重试。
    """


async def _call_dispatcher(dispatcher: Dispatcher | None, run_id: str, countdown: float) -> None:
    """投递重试消息。失败时抛 :class:`RetryPublicationError`（不得静默 ACK）。"""
    if dispatcher is None:
        from .dispatch import dispatch_run

        dispatcher = dispatch_run
    try:
        if countdown and countdown > 0:
            try:
                await dispatcher(run_id, countdown=countdown)
                return
            except TypeError:
                # dispatcher 不接受 countdown：退回无延迟投递（这不是投递失败）
                pass
        await dispatcher(run_id)
    except Exception as e:
        raise RetryPublicationError(
            f"重试投递失败 run_id={run_id} countdown={countdown}: {type(e).__name__}"
        ) from e


async def _heartbeat_loop(
    svc: RunService,
    run_id: str,
    worker_id: str,
    lease_seconds: float,
    interval: float,
    *,
    lock: Any = None,
    thread_id: str | None = None,
    lock_ttl_seconds: float | None = None,
) -> None:
    """执行期间续租：同时维护 DB ownership lease 与 Redis thread lock TTL。

    两层租约含义不同：
      - DB ``lease_expires_at`` 决定「谁有权把 run 标为 RUNNING/接管」；
      - Redis thread lock TTL 决定「谁有权执行同一条 LangGraph state lineage」。

    只续 DB 不续 Redis 时，长执行会在图跑完之前丢掉 thread lock，另一个 worker
    就能进入同一条 state lineage——这正是需要避免的并发写。失去锁后本 worker 不
    再静默继续：记录指标与告警日志，交由 lease 语义收口。
    """
    try:
        while True:
            await asyncio.sleep(max(1.0, interval))
            renewed = svc.heartbeat(run_id, worker_id=worker_id, lease_seconds=lease_seconds)
            metrics.record_worker_heartbeat(bool(renewed))
            if lock is not None and thread_id is not None and lock_ttl_seconds is not None:
                try:
                    ok = await lock.refresh(thread_id, worker_id, lock_ttl_seconds)
                except ThreadLockBackendError as e:
                    ok = False
                    logger.warning(
                        "thread lock 续租失败 run_id=%s thread=%s: %s",
                        run_id,
                        thread_id,
                        type(e).__name__,
                    )
                metrics.record_thread_lock_renewed(bool(ok))
                if not ok:
                    logger.warning(
                        "thread lock 续租失败（可能已被接管/过期）run_id=%s thread=%s",
                        run_id,
                        thread_id,
                    )
            if not renewed:
                return
    except asyncio.CancelledError:
        return
