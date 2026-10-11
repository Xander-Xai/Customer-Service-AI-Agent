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
from .run_service import RunNotFound, RunOwnershipLost, RunService
from .statuses import TERMINAL_STATUSES, InvalidRunTransition, RunStatus

logger = get_logger("runtime.executor")

RuntimeProvider = Callable[[], Awaitable[Any]]
Dispatcher = Callable[..., Awaitable[Any]]


def _new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _awaiting_approval(result: Any) -> bool:
    """执行结果是否表示图正挂在人工审批的 interrupt 上。

    LangGraph 在 interrupt 时不抛异常，而是在返回值注入 ``__interrupt__``。实测
    （langgraph 1.2.12）：该 key 非空 == 节点待重放 == 需要人工决策。
    """
    if not isinstance(result, dict):
        return False
    return bool(result.get("__interrupt__"))


def _build_resume_command(run_id: str) -> Any:
    """构造审批恢复用的 ``Command(resume=<decision>)``；无可消费决策时返回 None。

    ``ApprovalService.consume_resume`` 以 ``WHERE resumed_at IS NULL`` 原子认领，
    因此 at-least-once 重投递 / 多 worker 并发恢复时**只有一个**能拿到决策并真正
    恢复执行；其余返回 None，本次不碰 run 状态。

    返回 None 的三种情况：审批仍在 PENDING 未过期 / 已被他人消费 / 该 run 根本
    没有审批记录。前两种是正常等待，第三种说明状态机被外部改坏（保守不动）。
    """
    try:
        from core.hitl.approval_service import get_approval_service

        decision = get_approval_service().consume_resume(run_id)
    except Exception as e:  # pragma: no cover - 审批服务不可用时不误恢复
        logger.error(
            "读取审批决策失败 run_id=%s err=%s（不恢复，保持等待）",
            run_id,
            type(e).__name__,
        )
        return None
    if decision is None:
        return None
    try:
        from langgraph.types import Command
    except Exception:  # pragma: no cover - langgraph 必有 Command
        return None
    logger.info(
        "审批决策已消费 run_id=%s action=%s decision=%s approval=%s",
        run_id,
        decision.get("action"),
        decision.get("decision"),
        decision.get("approval_id"),
    )
    return Command(resume=decision)


async def _park_for_approval(
    svc: RunService, run_id: str, result: Any, *, expected_worker_id: str | None = None
) -> str:
    """RUNNING -> WAITING_APPROVAL：图挂起等人工决策。

    刻意**不算成功也不算失败**：既不 ``mark_succeeded``（副作用还没发生）也不进
    ``_handle_failure``（这不是失败，不该消耗 retry / 进 DLQ）。WAITING_APPROVAL
    是非终态，审批 API 决策后会重新 dispatch 该 run。

    ownership 丢失时同样只是读回当前状态：旧 worker 没有资格把新 owner 的 run
    挂起，也**不**因此把自己记成失败。
    """
    interrupt_payload = None
    if isinstance(result, dict):
        items = result.get("__interrupt__") or []
        if items:
            first = items[0]
            interrupt_payload = getattr(first, "value", None)
    try:
        svc.mark_waiting_approval(run_id, expected_worker_id=expected_worker_id)
    except RunOwnershipLost as lost:
        logger.warning(
            "挂起审批时 ownership 已丢失，放弃提交 run_id=%s expected_worker=%s current_status=%s",
            run_id,
            lost.expected_worker_id,
            lost.current_status,
        )
        latest = svc.get_run(run_id)
        return str(latest["status"]) if latest else "MISSING"
    except InvalidRunTransition:
        latest = svc.get_run(run_id)
        return str(latest["status"]) if latest else "MISSING"
    metrics.record_run_status(RunStatus.WAITING_APPROVAL.value)
    metrics.record_worker_task("waiting_approval")
    action = (
        (interrupt_payload or {}).get("action") if isinstance(interrupt_payload, dict) else None
    )
    logger.info("高风险操作挂起等待人工审批 run_id=%s action=%s", run_id, action)
    return RunStatus.WAITING_APPROVAL.value


def _runtime_accepts_resume(run_callable: Any) -> bool:
    """注入的 ``runtime.run()`` 是否接受 ``resume_command``。

    签名不可判定时（``**kwargs``、C 函数、mock）一律返回 True：宁可让下游自己
    报错，也不要在**没有**审批要恢复的普通路径上误判。
    """
    import inspect

    try:
        sig = inspect.signature(run_callable)
    except (TypeError, ValueError):
        return True
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "resume_command" and param.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
    return False


def _telemetry_span(name: str, *, attributes: dict[str, Any] | None = None):
    """Resolve the application span helper, with a last-resort no-op fallback.

    Two levels, deliberately. ``core.telemetry`` is the full implementation, but
    it is an ordinary import: if it were missing, syntactically broken, or caught in
    an import cycle, ``from core.telemetry import span`` would raise. This is not
    hypothetical — a diagnostic-only dependency failing to import must not be able
    to fail a business run, which is exactly what "tracing never changes AgentRun
    state, retry, approval or idempotency" requires.

    So the second level lives in ``core.tracing`` and depends on nothing but
    ``contextlib``. The blast radius of "tracing is broken" is then "there is no
    trace" rather than "there is no business".
    """
    try:
        from core.telemetry import span as _span
    except Exception:  # noqa: BLE001 - diagnostics must never break the runtime
        from core.tracing import safe_span as _span
    return _span(name, attributes=attributes)


def _pending_approval_identity(result: Any) -> tuple[str | None, str | None]:
    """Extract ``(approval_id, action)`` from an ``__interrupt__`` payload.

    Reads identifiers and the action name only — never the proposal. A proposal
    carries order numbers, refund amounts and customer identifiers, i.e. exactly
    the content that must not reach a trace backend.
    """
    if not isinstance(result, dict):
        return None, None
    items = result.get("__interrupt__") or []
    if not items:
        return None, None
    payload = getattr(items[0], "value", None)
    if not isinstance(payload, dict):
        return None, None
    approval_id = payload.get("approval_id")
    action = payload.get("action")
    return (str(approval_id) if approval_id else None, str(action) if action else None)


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
            resuming_approval = status == RunStatus.WAITING_APPROVAL.value

            # 审批恢复：先把该 run 已决策的审批**原子消费**掉，再决定是否真的恢复。
            # 消费不到（仍在 PENDING 未过期 / 已被别的 worker 消费）就不动 run 状态。
            resume_command = None
            if resuming_approval:
                resume_command = _build_resume_command(run_id)

            # mark_running 语义是「领取」，可能返回 None（他人持有有效 lease）；
            # mark_resumed_running 恒返回记录。显式标注联合类型，否则首个赋值分支
            # 会把 running 收窄成 dict，后续 `if running is None` 分支就变成
            # 不可达（mypy 会因此在 else 分支报 assignment 错误）。
            running: dict[str, Any] | None
            try:
                if resuming_approval and resume_command is not None:
                    # WAITING_APPROVAL -> RUNNING，且**不递增 attempt**
                    # （等待人不是失败，不能消耗 AGENT_RUN_MAX_ATTEMPTS）。
                    running = svc.mark_resumed_running(
                        run_id,
                        worker_id=owner,
                        task_id=task_id,
                        lease_seconds=cfg.AGENT_RUN_LEASE_SECONDS,
                    )
                elif resuming_approval:
                    # 没有可消费的决策：审批仍在等待（或已被他人恢复）。
                    logger.info("run=%s 仍无可消费的审批决策，保持 WAITING_APPROVAL", run_id)
                    return RunStatus.WAITING_APPROVAL.value
                else:
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
            # Lightweight tracing: ONE span per execution attempt, opened after the
            # run context exists so run/thread/task correlation attributes are
            # available, and closed in the ``finally`` below so every exit path
            # closes it — normal return, early return for WAITING_APPROVAL, the
            # exception path, and the heartbeat cancellation.
            #
            # Deliberately opened/closed by hand rather than with a ``with`` block:
            # wrapping the body would re-indent ~70 lines of state-machine code for
            # no behavioural gain, and hand-closing inside the existing ``finally``
            # is exactly as total.
            #
            # The span records outcome only — never the query, never tool arguments.
            # Tracing failures are swallowed inside ``core.telemetry`` and can never
            # change the state machine, retry accounting, approval or idempotency.
            #
            # HITL boundary: this segment and the post-approval resume segment are
            # **not** promised to be one span. A run can sit in WAITING_APPROVAL
            # until its TTL, and the decision arrives in a different request, so the
            # two are separate traces correlated by ``run_id`` / ``approval_id``
            # (see the module docstring of ``core/telemetry.py``).
            _span_cm = _telemetry_span(
                "csai.agent.execute.resume" if resume_command is not None else "csai.agent.execute",
                attributes={
                    "csai.run_id": run_id,
                    "csai.thread_id": thread_id,
                    "csai.task_id": task_id,
                    "csai.retry_attempt": (running or {}).get("attempt"),
                    **({"csai.resumed": True} if resume_command is not None else {}),
                },
            )
            trace_ctx = _span_cm.__enter__()
            try:
                if runtime_provider is None:
                    from .bootstrap import get_default_runtime

                    runtime_provider = get_default_runtime
                runtime = await runtime_provider()
                run_kwargs: dict[str, Any] = {
                    "thread_id": thread_id,
                    "query": run["query"],
                    "user_id": run.get("user_id"),
                }
                if resume_command is not None:
                    # 只在**确实要恢复审批**时才传该 kwarg：``runtime_provider`` 是
                    # 公开注入点，既有实现（测试替身 / 自定义 runtime）普遍按固定
                    # 签名实现 run()，无条件多传会让所有普通执行都 TypeError。
                    if not _runtime_accepts_resume(runtime.run):
                        # 静默忽略会把「审批已决策」吞掉：图永远不恢复，run 卡在
                        # WAITING_APPROVAL。宁可响亮失败（走 permanent -> DLQ 可观测）。
                        raise TypeError(
                            "注入的 runtime.run() 不支持 resume_command；"
                            "无法恢复人工审批（human-in-the-loop 需要该参数支持）"
                        )
                    run_kwargs["resume_command"] = resume_command
                result = await runtime.run(**run_kwargs)
            except Exception as e:
                error_type = classify_exception(e)
                error_code = type(e).__name__
                error_message = safe_error_message(e)
                with contextlib.suppress(Exception):
                    trace_ctx.set_attribute("csai.error_type", error_type)
                    trace_ctx.set_attribute("csai.error_code", error_code)
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
                if _awaiting_approval(result):
                    # 图挂在 interrupt 上等人工：不是成功，也不是失败。
                    # 转入 WAITING_APPROVAL（非终态），等审批 API 决策后 dispatch。
                    #
                    # tracing 边界：这一段与"审批后的恢复"**不**承诺是同一个 span。
                    # OTel context 不跨进程/跨长时间自动保持，run 可能在
                    # WAITING_APPROVAL 停留到 TTL。两段各自成 trace，通过
                    # run_id / approval_id 关联。
                    with contextlib.suppress(Exception):
                        trace_ctx.set_attribute("csai.run_status", RunStatus.WAITING_APPROVAL.value)
                        _approval_id, _risk = _pending_approval_identity(result)
                        if _approval_id:
                            trace_ctx.set_attribute("csai.approval_id", _approval_id)
                        trace_ctx.set_attribute("csai.risk_level", "high")
                    return await _park_for_approval(svc, run_id, result, expected_worker_id=owner)
                try:
                    svc.mark_succeeded(run_id, result, expected_worker_id=owner)
                except RunOwnershipLost as lost:
                    # 已被接管：这次执行的结果不再有权提交。读回当前状态退出，
                    # 绝不 mark_failed —— ownership 丢失不是业务失败。
                    #
                    # 这里返回**正常状态**（而不是抛异常），Celery 因此会 ACK，broker
                    # 不再投递该 run。若新 owner 尚未出现（只是本 worker 的 lease 过期），
                    # 该 run 就是「没有任何机制会再碰它」的孤儿——由 reconciler 扫描
                    # 过期 lease 的 RUNNING 收敛（见 ``runtime/retry.py``）。
                    metrics.record_ownership_lost_commit()
                    logger.warning(
                        "提交成功时 ownership 已丢失，放弃提交 run_id=%s "
                        "expected_worker=%s current_status=%s current_worker=%s",
                        run_id,
                        lost.expected_worker_id,
                        lost.current_status,
                        lost.current_worker_id,
                    )
                    latest = svc.get_run(run_id)
                    return str(latest["status"]) if latest else "MISSING"
                with contextlib.suppress(Exception):
                    trace_ctx.set_attribute("csai.run_status", RunStatus.SUCCEEDED.value)
                    trace_ctx.set_attribute(
                        "csai.duration_ms",
                        round((time.perf_counter() - exec_started) * 1000, 2),
                    )
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
                # Close the execution span last, so its duration covers the graph
                # run. Never let a tracing problem escape into the state machine.
                with contextlib.suppress(Exception):
                    _span_cm.__exit__(None, None, None)
        finally:
            if lock is not None:
                with contextlib.suppress(ThreadLockBackendError):
                    await lock.release(thread_id, owner)
    except RetryPublicationError:
        # 重试消息投递失败：run 已经在 RETRYING/QUEUED 等一条**不会到来**的消息。
        # 这里绝不能 ACK，也不能把它改成 FAILED（那会丢掉一次合法重试机会）。
        # 逃逸出任务 -> Celery acks_late 不 ACK -> broker 重新投递 -> 租约到期后被接管。
        logger.error("重试投递失败，让任务逃逸以触发 broker redelivery: run_id=%s", run_id)
        metrics.record_retry_publication_failure()
        raise
    except Exception as e:  # pragma: no cover - 未预期错误：标记失败避免卡死
        logger.error("execute_run 未预期异常 run_id=%s: %s", run_id, type(e).__name__)
        with contextlib.suppress(Exception):
            latest = svc.get_run(run_id)
            if latest is not None and latest["status"] == RunStatus.RUNNING.value:
                try:
                    svc.mark_failed(
                        run_id,
                        error_code=type(e).__name__,
                        error_message=safe_error_message(e),
                        error_type="permanent",
                        expected_worker_id=owner,
                    )
                    metrics.record_worker_task("failed")
                except RunOwnershipLost as lost:
                    # 已被接管：不能把新 owner 的 run 写成 FAILED，也不记失败。
                    return await _abandon_on_ownership_lost(svc, run_id, lost, owner)
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


async def _abandon_on_ownership_lost(
    svc: RunService, run_id: str, lost: RunOwnershipLost, owner: str | None
) -> str:
    """失去所有权后的唯一动作：读回当前状态并退出。

    刻意**不**做任何状态写入：不 mark_failed、不 mark_retrying、不进 DLQ、
    不发布重试消息、不消耗 attempt。另一个 worker 已经拥有执行权，把
    「我不是 owner 了」记成业务失败会污染它正在跑的 run。

    只记录 bounded warning：run_id / expected_worker_id / current_status /
    current_worker_id。**不含** query、result、tool args 或任何用户数据。
    """
    logger.warning(
        "ownership 已丢失，放弃本次状态提交（不记失败、不重试、不进 DLQ）"
        " run_id=%s expected_worker=%s current_status=%s current_worker=%s",
        run_id,
        lost.expected_worker_id,
        lost.current_status,
        lost.current_worker_id,
    )
    latest = svc.get_run(run_id)
    return str(latest["status"]) if latest else "MISSING"


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
        try:
            svc.mark_failed(
                run_id,
                error_code=error_code,
                error_message=error_message,
                error_type=error_type,
                expected_worker_id=worker_id,
            )
        except RunOwnershipLost as lost:
            # 已被接管：旧 worker 无权把新 owner 的 run 写成 FAILED。
            return await _abandon_on_ownership_lost(svc, run_id, lost, worker_id)
        except InvalidRunTransition:
            pass
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
        try:
            svc.mark_dead_letter(
                run_id,
                error_code=error_code,
                error_message=error_message,
                error_type=error_type,
                worker_id=worker_id,
                expected_worker_id=worker_id,
            )
        except RunOwnershipLost as lost:
            return await _abandon_on_ownership_lost(svc, run_id, lost, worker_id)
        except InvalidRunTransition:
            pass
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
            expected_worker_id=worker_id,
        )
    except RunOwnershipLost as lost:
        # 关键：ownership 丢失**不**发布重试消息。否则新 owner 正在执行，
        # 旧 worker 又投一条 retry，会凭空多出一次执行与一次 attempt 消耗。
        return await _abandon_on_ownership_lost(svc, run_id, lost, worker_id)
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

    续租失败**不得**终止循环
    ------------------------
    早期实现只捕获 ``ThreadLockBackendError`` 与 ``CancelledError``。于是 ``heartbeat()``
    抛出任何瞬时数据库异常（连接重置、序列化失败、一次超时）时，异常会穿出协程、
    续租就此**永久停止**，而调用方在 ``finally`` 里
    ``suppress(asyncio.CancelledError, Exception)`` 地 await 它，于是异常连日志都不会有。

    后果不是「这次没续上」，而是确定的活性缺陷：worker 继续把图跑完，lease 过期后
    它的 ``mark_succeeded`` 被 owner CAS 正确拒绝，任务正常返回被 ACK，
    而没有任何机制会再投递这个 run——它永久停在 RUNNING。

    因此这里按轮次隔离异常：记录有界告警后**继续下一轮**。真正的失效信号是
    ``renewed is False``（lease 已被接管），那才 return。
    """
    consecutive_errors = 0
    try:
        while True:
            await asyncio.sleep(max(1.0, interval))
            # lock 续租独立于 DB 续租：DB 抖动不该顺带丢掉 thread lock。
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

            try:
                renewed = svc.heartbeat(run_id, worker_id=worker_id, lease_seconds=lease_seconds)
            except Exception as e:  # noqa: BLE001 - 续租失败必须继续下一轮
                consecutive_errors += 1
                metrics.record_worker_heartbeat_error()
                logger.warning(
                    "lease 续租异常，继续重试 run_id=%s consecutive=%s err=%s",
                    run_id,
                    consecutive_errors,
                    type(e).__name__,
                )
                continue

            if consecutive_errors:
                logger.info("lease 续租已恢复 run_id=%s after=%s", run_id, consecutive_errors)
            consecutive_errors = 0
            metrics.record_worker_heartbeat(bool(renewed))
            if not renewed:
                # lease 已被接管 / 已失效：停止续租，不与新 owner 竞争。
                return
    except asyncio.CancelledError:
        return
