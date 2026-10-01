"""异步 Agent Run API（Hybrid Architecture）。

快路径保留：``POST /api/chat`` / ``POST /api/chat/stream``（FastAPI -> LangGraph
-> SSE），适合低延迟客服问答。

长任务路径（本模块）::

    POST /api/runs              -> 202，创建 RunRecord(QUEUED) + 入队，立即返回 run_id
    GET  /api/runs/{run_id}     -> 查询状态与结果（polling）
    GET  /api/runs/dead         -> 管理员查询 application-level DLQ

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


class CreateRunRequest(BaseModel):
    query: str = Field(..., max_length=MAX_QUERY_LENGTH)
    session_id: str = Field(default="", max_length=36)
    session_token: str = Field(default="", max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=128)


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
