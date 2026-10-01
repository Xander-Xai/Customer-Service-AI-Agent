"""异步 Agent Run API。

POST /api/runs                    -> 202，创建并入队（返回 run_id/status/thread_id）
GET  /api/runs/{run_id}           -> 查询状态与结果
POST /api/runs/{run_id}/cancel    -> 取消尚未开始的 run

API 只负责「创建 + 入队」，不拥有执行生命周期；执行在 Celery worker。
现有 POST /api/chat 等同步接口保持不变。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from api.utils import extract_user_id, sanitize_input
from core.config import MAX_QUERY_LENGTH
from core.logger import get_logger, get_trace_id
from runtime import metrics as run_metrics
from runtime.run_service import RunNotFound, RunService, build_idempotency_scope
from runtime.statuses import InvalidRunTransition, RunStatus

router = APIRouter()
logger = get_logger("api.runs")

_IDEMPOTENCY_ENDPOINT = "POST:/api/runs"


class CreateRunRequest(BaseModel):
    query: str = Field(..., max_length=MAX_QUERY_LENGTH)
    session_id: str = Field(default="", max_length=36)
    session_token: str = Field(default="", max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=128)


def _get_service(request: Request) -> RunService:
    service = getattr(request.app.state, "run_service", None)
    if service is not None:
        return service
    from runtime.run_service import get_run_service

    return get_run_service()


def _serialize(run: dict) -> dict:
    return {
        "run_id": run["id"],
        "thread_id": run["thread_id"],
        "session_id": run["session_id"],
        "user_id": run["user_id"],
        "status": run["status"],
        "result": run["result"],
        "error_code": run["error_code"],
        "error_message": run["error_message"],
        "attempt": run["attempt"],
        "max_attempts": run["max_attempts"],
        "error_type": run.get("error_type"),
        "next_retry_at": run["next_retry_at"].isoformat() if run.get("next_retry_at") else None,
        "created_at": run["created_at"].isoformat() if run["created_at"] else None,
        "queued_at": run["queued_at"].isoformat() if run["queued_at"] else None,
        "started_at": run["started_at"].isoformat() if run["started_at"] else None,
        "finished_at": run["finished_at"].isoformat() if run["finished_at"] else None,
        "trace_id": run["trace_id"],
    }


def _check_ownership(request: Request, run: dict) -> bool:
    if getattr(request.app.state, "dev_mode", False):
        return True
    owner = run.get("user_id")
    if not owner:
        return True  # 匿名 run 向后兼容
    return extract_user_id(request) == owner


@router.post("/api/runs")
async def create_run(
    data: CreateRunRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    from api.routes.chat import _SessionValidationError, get_authenticated_session

    try:
        session = await get_authenticated_session(
            request, data.session_id, data.session_token
        )
    except _SessionValidationError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)

    query = sanitize_input(data.query)[:MAX_QUERY_LENGTH]
    if not query:
        return JSONResponse({"error": "query 不能为空"}, status_code=400)

    service = _get_service(request)
    raw_key = (idempotency_key or data.idempotency_key or "").strip()
    scoped_key = (
        build_idempotency_scope(session.user_id, _IDEMPOTENCY_ENDPOINT, raw_key)
        if raw_key
        else None
    )

    # 幂等命中：同 user + endpoint + key 不重复创建/入队，返回原 run_id
    if scoped_key:
        existing = service.get_by_idempotency_key(scoped_key)
        if existing is not None:
            run_metrics.record_idempotency_hit()
            return JSONResponse(_serialize(existing), status_code=202)

    run = service.create_run(
        query=query,
        session_id=session.sid,
        user_id=session.user_id,
        idempotency_key=scoped_key,
        trace_id=get_trace_id(),
    )

    # 并发竞态下 create 内部命中已有 run：直接返回，不重复入队
    if run["status"] != RunStatus.PENDING.value:
        run_metrics.record_idempotency_hit()
        return JSONResponse(_serialize(run), status_code=202)

    service.mark_queued(run["id"])

    from runtime import dispatch
    from runtime.event_publisher import publish_run_event

    # best-effort：把 run_queued 写入事件流（失败不影响入队）
    await publish_run_event(
        run["id"], run["thread_id"], "run_queued", {"session_id": run["session_id"]}
    )

    try:
        await dispatch.dispatch_run(run["id"])
    except Exception as e:
        logger.error("run 入队失败 run_id=%s: %s", run["id"], type(e).__name__)
        with contextlib.suppress(InvalidRunTransition):
            service.mark_dead(
                run["id"], error_code="DISPATCH_FAILED", error_message=str(e)
            )
        return JSONResponse({"error": "任务调度失败，请稍后重试"}, status_code=503)

    run = service.get_run(run["id"]) or run
    return JSONResponse(
        {
            "run_id": run["id"],
            "status": run["status"],
            "thread_id": run["thread_id"],
        },
        status_code=202,
    )


@router.get("/api/runs/dead")
async def list_dead_runs(request: Request, limit: int = 100):
    """管理员观测：DEAD run 列表（脱敏，不含 secret）。"""
    from api.routes.monitoring import _require_monitoring_auth

    _require_monitoring_auth(request)
    service = _get_service(request)
    limit = min(max(limit, 1), 500)
    runs = service.list_dead(limit=limit)
    return {"dead": [_serialize(r) for r in runs], "count": len(runs)}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    service = _get_service(request)
    try:
        run = service.require_run(run_id)
    except RunNotFound:
        return JSONResponse({"error": "run 不存在"}, status_code=404)
    if not _check_ownership(request, run):
        return JSONResponse({"error": "无权访问该 run"}, status_code=403)
    return _serialize(run)


@router.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request):
    service = _get_service(request)
    try:
        run = service.require_run(run_id)
    except RunNotFound:
        return JSONResponse({"error": "run 不存在"}, status_code=404)
    if not _check_ownership(request, run):
        return JSONResponse({"error": "无权取消该 run"}, status_code=403)
    try:
        run = service.cancel_run(run_id)
    except InvalidRunTransition as e:
        return JSONResponse(
            {"error": str(e), "status": run["status"]}, status_code=409
        )
    return {"run_id": run["id"], "status": run["status"], "thread_id": run["thread_id"]}


# ── 跨进程事件流 SSE ──

_FINAL_STATUSES = {
    RunStatus.SUCCEEDED.value,
    RunStatus.CANCELLED.value,
    RunStatus.DEAD.value,
}


def _run_is_final(run: dict) -> bool:
    status = run.get("status")
    if status in _FINAL_STATUSES:
        return True
    # FAILED 且未安排 retry = 终态
    return status == RunStatus.FAILED.value and not run.get("next_retry_at")


def _final_event_type(run: dict) -> str:
    status = run.get("status")
    if status == RunStatus.SUCCEEDED.value:
        return "run_completed"
    if status == RunStatus.CANCELLED.value:
        return "run_cancelled"
    return "run_failed"


def _sse_frame(event_id: str | None, data: dict) -> str:
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _final_sse_from_run(run: dict) -> str:
    data = {
        "type": _final_event_type(run),
        "run_id": run["id"],
        "thread_id": run["thread_id"],
        "status": run["status"],
        "result": run.get("result"),
        "error_code": run.get("error_code"),
        "error_message": run.get("error_message"),
        "source": "db_final",
    }
    return _sse_frame("db-final", data)


async def _run_event_sse_generator(request: Request, service: RunService, run_id: str, last_event_id: str | None):
    """读取事件流 -> SSE。Stream 过期/缺失时回退到 PostgreSQL 最终状态。"""
    from core.config import (
        RUN_EVENT_SSE_BLOCK_MS,
        RUN_EVENT_SSE_COUNT,
        RUN_EVENT_SSE_IDLE_TIMEOUT_SECONDS,
        RUN_EVENT_SSE_POLL_DB_SECONDS,
    )
    from runtime.event_stream import get_run_event_stream
    from runtime.events import RunEvent, is_terminal_type

    run_metrics.inc_sse_connections()
    if last_event_id:
        run_metrics.record_sse_reconnect()

    stream = get_run_event_stream()
    blocking = bool(getattr(stream, "is_blocking", False))
    cursor = last_event_id or "0-0"
    idle_seconds = 0.0
    last_db_check = time.time()

    try:
        # 已终态：直接给最终状态（Stream 可能已过期）
        current = service.get_run(run_id)
        if current is not None and _run_is_final(current):
            yield _final_sse_from_run(current)
            return

        while True:
            if await request.is_disconnected():
                return
            try:
                entries = await stream.read(
                    run_id,
                    after_id=cursor,
                    block_ms=RUN_EVENT_SSE_BLOCK_MS,
                    count=RUN_EVENT_SSE_COUNT,
                )
            except Exception as e:  # Redis 抖动：回退 DB 轮询
                logger.debug("event stream read 失败 run=%s: %s", run_id, type(e).__name__)
                entries = []

            if entries:
                idle_seconds = 0.0
                for entry in entries:
                    cursor = entry.event_id
                    event = RunEvent.from_fields(entry.fields, entry.event_id)
                    run_metrics.observe_event_lag(time.time() - event.timestamp)
                    yield _sse_frame(event.event_id, event.to_sse_dict())
                    if is_terminal_type(event.type):
                        return
            else:
                if blocking:
                    idle_seconds += RUN_EVENT_SSE_BLOCK_MS / 1000.0
                else:
                    await asyncio.sleep(0.02)
                    idle_seconds += 0.02

            # 周期性 DB 终态检查（覆盖丢失的终态事件 / 过期 stream）
            now = time.time()
            if now - last_db_check >= RUN_EVENT_SSE_POLL_DB_SECONDS:
                last_db_check = now
                current = service.get_run(run_id)
                if current is not None and _run_is_final(current):
                    yield _final_sse_from_run(current)
                    return

            if idle_seconds >= RUN_EVENT_SSE_IDLE_TIMEOUT_SECONDS:
                current = service.get_run(run_id)
                if current is not None and _run_is_final(current):
                    yield _final_sse_from_run(current)
                return
    finally:
        run_metrics.dec_sse_connections()


@router.get("/api/runs/{run_id}/stream")
async def stream_run_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """跨进程 Agent 事件流 SSE。

    - 读取 Redis Stream（worker 发布），转 SSE；
    - 支持 ``Last-Event-ID`` 断线续读；
    - Stream 过期后回退 PostgreSQL 最终状态；
    - 校验 run ownership。
    """
    service = _get_service(request)
    try:
        run = service.require_run(run_id)
    except RunNotFound:
        return JSONResponse({"error": "run 不存在"}, status_code=404)
    if not _check_ownership(request, run):
        return JSONResponse({"error": "无权访问该 run"}, status_code=403)

    return StreamingResponse(
        _run_event_sse_generator(request, service, run_id, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
