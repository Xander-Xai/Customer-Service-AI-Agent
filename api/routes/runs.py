"""异步 Agent Run API（Hybrid Architecture）。

快路径保留：``POST /api/chat`` / ``POST /api/chat/stream``（FastAPI -> LangGraph
-> SSE），适合低延迟客服问答。

长任务路径（本模块）::

    POST /api/runs                 -> 202，创建 AgentRun(QUEUED) + 入队，立即返回 run_id
    GET  /api/runs/{run_id}        -> 查询状态与结果（polling）
    GET  /api/runs/{run_id}/events -> SSE 转发 run 事件流（支持 Last-Event-ID 续读）
    POST /api/runs/{run_id}/cancel -> 协作式取消未完成的 run -> CANCELLED
    GET  /api/runs/dead            -> 管理员查询 application-level DLQ

API 只负责「创建 + 入队」，不拥有执行生命周期；执行在独立 Celery worker。
"""

from __future__ import annotations

import contextlib
from typing import cast

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.utils import extract_user_id, sanitize_input
from core.config import MAX_QUERY_LENGTH
from core.logger import get_logger, get_trace_id
from runtime import metrics as run_metrics
from runtime.run_service import RunNotFound, RunService, build_idempotency_scope

router = APIRouter()
logger = get_logger("api.runs")

_IDEMPOTENCY_ENDPOINT = "POST:/api/runs"

#: 调用方可提交的原始 Idempotency-Key 上限。header 与 body **必须一致** —— 之前
#: body 有 128 上限而 ``Idempotency-Key`` header 完全没有校验，导致契约不统一。
IDEMPOTENCY_KEY_INPUT_MAX = 128


class CreateRunRequest(BaseModel):
    query: str = Field(..., max_length=MAX_QUERY_LENGTH)
    session_id: str = Field(default="", max_length=36)
    session_token: str = Field(default="", max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=IDEMPOTENCY_KEY_INPUT_MAX)


def _get_service(request: Request) -> RunService:
    service = getattr(request.app.state, "run_service", None)
    if service is not None:
        return cast(RunService, service)
    from runtime.run_service import get_run_service

    return get_run_service()


def _serialize(run: dict) -> dict:
    def _iso(value):
        return value.isoformat() if value else None

    return {
        "run_id": run["id"],
        "thread_id": run["thread_id"],
        "session_id": run["session_id"],
        "user_id": run["user_id"],
        "status": run["status"],
        "result": run["result"],
        "error_code": run["error_code"],
        "error_message": run["error_message"],
        "error_type": run.get("error_type"),
        "attempt": run["attempt"],
        "max_attempts": run["max_attempts"],
        "next_retry_at": _iso(run.get("next_retry_at")),
        "created_at": _iso(run["created_at"]),
        "queued_at": _iso(run["queued_at"]),
        "started_at": _iso(run["started_at"]),
        "finished_at": _iso(run["finished_at"]),
        "trace_id": run["trace_id"],
        "task_id": run.get("task_id"),
    }


def _check_ownership(request: Request, run: dict) -> bool:
    if getattr(request.app.state, "dev_mode", False):
        return True
    owner = run.get("user_id")
    if not owner:
        return True  # 匿名 run 向后兼容
    return bool(extract_user_id(request) == owner)


@router.post("/api/runs")
async def create_run(
    data: CreateRunRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    from api.routes.chat import _SessionValidationError, get_authenticated_session

    try:
        session = await get_authenticated_session(request, data.session_id, data.session_token)
    except _SessionValidationError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)

    query = sanitize_input(data.query)[:MAX_QUERY_LENGTH]
    if not query:
        return JSONResponse({"error": "query 不能为空"}, status_code=400)

    service = _get_service(request)
    raw_key = (idempotency_key or data.idempotency_key or "").strip()
    if len(raw_key) > IDEMPOTENCY_KEY_INPUT_MAX:
        # header 与 body 走同一契约：超限直接 400，而不是等 DB 报 value too long。
        return JSONResponse(
            {"error": f"Idempotency-Key 长度超限（上限 {IDEMPOTENCY_KEY_INPUT_MAX}）"},
            status_code=400,
        )
    scoped_key = (
        build_idempotency_scope(session.user_id, _IDEMPOTENCY_ENDPOINT, raw_key)
        if raw_key
        else None
    )

    # 幂等命中：同 user + endpoint + key 不重复创建/入队，返回原 run
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

    from runtime import dispatch

    try:
        await dispatch.dispatch_run(run["id"])
    except Exception as e:
        logger.error("run 入队失败 run_id=%s: %s", run["id"], type(e).__name__)
        with contextlib.suppress(Exception):
            service.mark_dead_letter(
                run["id"],
                error_code="DISPATCH_FAILED",
                error_message=str(e),
                error_type="transient",
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
    """管理员观测：application-level DLQ 记录（脱敏，不含 secret）。"""
    from api.routes.monitoring import _require_monitoring_auth

    _require_monitoring_auth(request)
    service = _get_service(request)
    limit = min(max(limit, 1), 500)
    records = service.list_dead_letters(limit=limit)
    serialized = [
        {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in rec.items()}
        for rec in records
    ]
    return {"dead_letters": serialized, "count": len(serialized)}


@router.get("/api/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = None,
    replay: bool = True,
):
    """SSE 转发 run 事件流（Redis Stream -> 客户端）。

    - 事件由 worker 写入 ``agent:run:{run_id}:events``，API 只做转发（worker 不接触
      浏览器连接）；
    - 支持断点续读：``Last-Event-ID`` 请求头或 ``?last_event_id=`` 指定 Redis stream
      entry id；``replay=false`` 时只看新事件（``$``）；
    - run 进入终态后自动关闭连接。

    **语义边界**：best-effort resumable event stream; **not** exactly-once; duplicates or gaps may occur around reconnect/replay; trimmed historical events may become unrecoverable。
    单条连接内每个事件只转发一次；但用较旧的 ``Last-Event-ID`` 重连会重放已处理过
    的事件（重复），``replay=false`` 与 idle 超时会造成缺口，``MAXLEN`` 近似裁剪
    后的历史不可恢复。权威状态请用 ``GET /api/runs/{run_id}``。
    """
    from fastapi.responses import StreamingResponse

    from runtime.events import (
        EVENT_CANCELLED,
        EVENT_COMPLETED,
        EVENT_FAILED,
        format_sse,
        read_events,
    )

    service = _get_service(request)
    try:
        run = service.require_run(run_id)
    except RunNotFound:
        return JSONResponse({"error": "run 不存在"}, status_code=404)
    if not _check_ownership(request, run):
        return JSONResponse({"error": "无权访问该 run"}, status_code=403)

    terminal = {"SUCCEEDED", "FAILED", "DEAD_LETTER", "CANCELLED"}
    start_from = last_event_id or request.headers.get("last-event-id") or "0-0"
    if not replay:
        start_from = "$"

    async def event_source():
        import redis.asyncio as aioredis

        from core.config import REDIS_URL

        client = aioredis.from_url(REDIS_URL, decode_responses=True)
        cursor = start_from
        idle_rounds = 0
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    events = await read_events(
                        client, run_id, last_event_id=cursor, block_ms=1000, count=100
                    )
                except Exception as e:
                    logger.warning("run 事件流读取失败 run_id=%s: %s", run_id, type(e).__name__)
                    yield format_sse(
                        "0-0", {"event": "error", "reason": type(e).__name__}
                    )
                    break

                for event_id, fields in events:
                    cursor = event_id
                    yield format_sse(event_id, fields)
                    name = fields.get("event")
                    if name in (EVENT_COMPLETED, EVENT_FAILED, EVENT_CANCELLED):
                        idle_rounds = 0
                        return

                idle_rounds += 1
                # run 已是终态且没有新事件 -> 收尾，避免连接悬挂
                try:
                    current = service.require_run(run_id)
                except RunNotFound:
                    break
                if current["status"] in terminal:
                    break
                if idle_rounds > 30:
                    yield format_sse(
                        "0-0",
                        {"event": "error", "reason": "idle_timeout", "status": current["status"]},
                    )
                    break
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
    """取消尚未完成的 run。

    协作式取消：立即把状态置 ``CANCELLED``（终态），未开始执行的 run 不会再被执行
    （worker 领取时因状态非可执行而跳过）。已进入 RUNNING 的 run **不会**被强行中断，
    其 ``mark_succeeded`` 走条件更新，不会覆盖 ``CANCELLED``。
    """
    service = _get_service(request)
    try:
        run = service.require_run(run_id)
    except RunNotFound:
        return JSONResponse({"error": "run 不存在"}, status_code=404)
    if not _check_ownership(request, run):
        return JSONResponse({"error": "无权取消该 run"}, status_code=403)

    before = run["status"]
    cancelled = service.cancel_run(run_id)
    after = cancelled["status"]
    run_metrics.record_run_status(after)
    if before != after:
        logger.info("run 已取消 run_id=%s %s -> %s", run_id, before, after)
    payload = _serialize(cancelled)
    payload["previous_status"] = before
    payload["was_terminal"] = before == after
    return payload
