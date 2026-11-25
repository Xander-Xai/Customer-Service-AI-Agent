"""
聊天相关路由：REST / SSE 流式 / 多模态图片 / 语音 / TTS / 文件上传
从 api/app.py create_app() 提取，通过 request.app.state 访问依赖。
"""

import asyncio
import json
import os  # noqa: F401 — 保留给 test_tts_voices_list mock 路径: api.routes.chat.os.getenv
import time
from dataclasses import dataclass

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from api.utils import extract_user_id, sanitize_input, validate_session_id
from core.concurrency.distributed_lock import ThreadBusyError, thread_busy_payload
from core.config import MAX_QUERY_LENGTH
from core.logger import get_logger

router = APIRouter()
logger = get_logger("api.chat")

# ===== SSE 流式常量 =====
SSE_CHUNK_TIMEOUT = 15.0  # SSE idle heartbeat interval (seconds)
SSE_QUEUE_MAX_EVENTS = 64  # bounded per-stream event count, not a byte budget
CHAT_QUERY_MAX_LENGTH = 2000  # 聊天查询最大字符数

# ===== 通用错误消息常量 =====
ERR_INTERNAL = "服务内部错误，请稍后重试"
ERR_QUERY_EMPTY = "query 不能为空"


# ── Pydantic 模型 ──


class ChatRequest(BaseModel):
    query: str = Field(..., max_length=CHAT_QUERY_MAX_LENGTH)
    session_id: str = Field(default="", max_length=36)
    session_token: str = Field(default="", max_length=64)

    @field_validator("session_id", "session_token", mode="before")
    @classmethod
    def coerce_none_to_empty(cls, v: object) -> object:
        return "" if v is None else v


class ChatStreamRequest(BaseModel):
    """SSE 流式输出请求模型"""

    query: str = Field(..., max_length=CHAT_QUERY_MAX_LENGTH)
    session_id: str = Field(default="", max_length=36)
    session_token: str = Field(default="", max_length=64)

    @field_validator("session_id", "session_token", mode="before")
    @classmethod
    def coerce_none_to_empty(cls, v: object) -> object:
        return "" if v is None else v


# ── SSE 工具 ──


def _sse_event(event_data: dict) -> str:
    return f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"


# ── Task 2.6: 共享认证 + 会话验证 ──


class _SessionValidationError(Exception):
    """内部异常：会话验证失败，路由捕获后返回 JSONResponse。"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


@dataclass
class AuthenticatedSession:
    """经过认证和验证的会话信息"""

    sid: str
    session_manager: object
    client_provided_sid: bool
    request: Request
    user_id: str | None = None


async def get_authenticated_session(
    request: Request,
    session_id: str = "",
    session_token: str = "",
) -> AuthenticatedSession:
    """
    共享依赖：验证 session_id 格式、检查会话令牌、设置 user_id。
    所有需要会话的端点调用此函数，失败时抛出 _SessionValidationError。

    P0-04: user_id 在此可信请求边界一次性提取（extract_user_id 从 JWT sub
    字段取得），并通过返回的 AuthenticatedSession.user_id 暴露给所有
    transport。路由必须使用 session.user_id 传入 run_graph，不得从
    request body / query / prompt 重新解析身份。
    """
    state = request.app.state
    session_manager = state.session_manager

    from core.industrial import industrial_enabled

    if industrial_enabled():
        from auth.router import get_current_user_from_request
        from core.session.ownership import (
            SessionOwnerDenied,
            SessionOwnerUnavailable,
            create_owned_session,
            require_session_owner,
        )

        actor = await asyncio.to_thread(get_current_user_from_request, request)
        if actor is None:
            raise _SessionValidationError(401, "Authenticated user required")
        user_id = str(actor.id)
        if session_manager is None:
            raise _SessionValidationError(503, "Session service unavailable")
        try:
            if session_id:
                if not session_manager.validate_session_token(session_id, session_token):
                    raise SessionOwnerDenied("Invalid session token")
                await require_session_owner(session_id, user_id)
                sid = session_id
            else:
                sid = await create_owned_session(user_id)
        except SessionOwnerDenied as exc:
            raise _SessionValidationError(403, "Session ownership not verified") from exc
        except SessionOwnerUnavailable as exc:
            raise _SessionValidationError(503, "Session ownership unavailable") from exc
        return AuthenticatedSession(sid, session_manager, bool(session_id), request, user_id)

    sid = validate_session_id(session_id)
    client_provided_sid = bool(session_id)

    if (
        client_provided_sid
        and session_manager
        and not session_manager.validate_session_token(sid, session_token or "")
    ):
        raise _SessionValidationError(403, "会话令牌无效")

    user_id = extract_user_id(request)
    if session_manager and user_id:
        session_manager.set_user_id(sid, user_id)

    return AuthenticatedSession(
        sid=sid,
        session_manager=session_manager,
        client_provided_sid=client_provided_sid,
        request=request,
        user_id=user_id,
    )


# ── Task 2.1: 共享 SSE 流式生成器 ──


@dataclass
class SSEStreamContext:
    """SSE 流式会话的共享上下文"""

    graph_task: asyncio.Task
    chunk_queue: asyncio.Queue
    sid: str
    session_manager: object
    client_provided_sid: bool
    status_msg: str
    progress_msg: str


def _build_sse_stream_context(
    request: Request,
    sid: str,
    query: str,
    session_manager: object,
    client_provided_sid: bool,
    multimodal_content: list | None = None,
    user_id: str | None = None,
) -> tuple[asyncio.Task, asyncio.Queue]:
    """构建 SSE 流式任务和队列，返回 (graph_task, chunk_queue)。

    P0-04: 必须显式传递 user_id（来自可信请求边界），禁止 client body
    或 prompt 覆盖身份。
    """
    run_graph = request.app.state.run_graph
    chunk_queue: asyncio.Queue = asyncio.Queue(maxsize=SSE_QUEUE_MAX_EVENTS)

    async def stream_callback(event: dict):
        await chunk_queue.put(event)

    kwargs: dict = {"stream_callback": stream_callback, "user_id": user_id}
    if multimodal_content is not None:
        kwargs["multimodal_content"] = multimodal_content
    graph_task = asyncio.create_task(run_graph(sid, query, **kwargs))
    return graph_task, chunk_queue


async def _stop_stream_graph(task: asyncio.Task) -> None:
    # Starlette can cancel the streaming scope on a disconnect. Shield cleanup
    # so the graph's finally clauses (including the thread lock) can finish.
    with anyio.CancelScope(shield=True):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class ManagedSSE(StreamingResponse):
    """Own the producer even if sending the first HTTP frame fails."""

    def __init__(self, ctx: SSEStreamContext, **kwargs):
        self.graph_task = ctx.graph_task
        super().__init__(_sse_stream_generator(ctx), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await _stop_stream_graph(self.graph_task)
                await self.body_iterator.aclose()


async def _sse_stream_generator(ctx: SSEStreamContext):
    """共享的 SSE 事件生成器，供 stream_chat 和 stream_multimodal_chat 复用。"""
    start_time = time.time()
    try:
        yield _sse_event({"type": "status", "content": ctx.status_msg})
        yield _sse_event({"type": "progress", "content": ctx.progress_msg})

        while not ctx.graph_task.done() or not ctx.chunk_queue.empty():
            if not ctx.chunk_queue.empty():
                event = ctx.chunk_queue.get_nowait()
            else:
                queue_task = asyncio.create_task(ctx.chunk_queue.get())
                try:
                    done, _ = await asyncio.wait(
                        {queue_task, ctx.graph_task},
                        timeout=SSE_CHUNK_TIMEOUT,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        # SSE comment; does not pretend that a business step completed.
                        yield ": heartbeat\n\n"
                        continue
                    if queue_task not in done:
                        # The graph finished; drain any already queued events.
                        continue
                    event = queue_task.result()
                finally:
                    # Never await the still-running graph in the per-event loop.
                    if not queue_task.done():
                        queue_task.cancel()
                        with anyio.CancelScope(shield=True):
                            await asyncio.gather(queue_task, return_exceptions=True)

            if event is None:
                break
            if isinstance(event, dict) and event.get("type") in (
                "chunk",
                "status",
                "thinking",
                "tool_call",
                "tool_result",
                "rag_status",
                "agent_switch",
                "content_complete",
            ):
                yield _sse_event(event)

        try:
            result = await ctx.graph_task
        except ThreadBusyError as e:
            logger.info("SSE THREAD_BUSY sid=%s", ctx.sid)
            yield _sse_event(
                {
                    "type": "error",
                    "code": "THREAD_BUSY",
                    "content": thread_busy_payload(e)["message"],
                }
            )
            return
        except Exception as e:
            logger.error(f"SSE 图执行失败: {e}", exc_info=True)
            yield _sse_event({"type": "error", "content": ERR_INTERNAL})
            return

        session_token = ""
        if ctx.session_manager:
            session_token = ctx.session_manager.generate_session_token(ctx.sid)

        elapsed = round(time.time() - start_time, 3)
        yield _sse_event(
            {
                "type": "done",
                "content": result.get("response", ""),
                "agent": result.get("current_agent", ""),
                "mode": result.get("collaboration_mode", "sequential"),
                "elapsed": elapsed,
                "cached": result.get("cached", False),
                "agents_used": result.get("agents_used", []),
                "resolution_status": result.get("resolution_status", ""),
                "handoff_ticket_id": result.get("handoff_ticket_id", ""),
                "handoff_status": result.get("handoff_status", ""),
                "session_id": ctx.sid,
                "session_token": session_token,
            }
        )
    except Exception as e:
        logger.error(f"SSE 流式处理失败: {e}", exc_info=True)
        yield _sse_event({"type": "error", "content": ERR_INTERNAL})
    finally:
        await _stop_stream_graph(ctx.graph_task)


# ── REST 聊天 ──


@router.post("/api/chat")
async def rest_chat(data: ChatRequest, request: Request):
    try:
        session = await get_authenticated_session(
            request,
            data.session_id,
            data.session_token,
        )
    except _SessionValidationError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)

    run_graph = request.app.state.run_graph
    query = sanitize_input(data.query)[:MAX_QUERY_LENGTH]
    if not query:
        return JSONResponse({"error": ERR_QUERY_EMPTY}, status_code=400)

    try:
        result = await run_graph(session.sid, query, user_id=session.user_id)
        # 统一字段映射（对齐 WS/SSE 端点）
        mapped = {
            "response": result.get("response", ""),
            "agent": result.get("current_agent", ""),
            "mode": result.get("collaboration_mode", "sequential"),
            "elapsed": result.get("elapsed", 0),
            "cached": result.get("cached", False),
            "agents_used": result.get("agents_used", []),
            "resolution_status": result.get("resolution_status", ""),
            "handoff_ticket_id": result.get("handoff_ticket_id", ""),
            "handoff_status": result.get("handoff_status", ""),
            "session_id": session.sid,
        }
        if session.session_manager:
            mapped["session_token"] = session.session_manager.generate_session_token(session.sid)
        return mapped
    except ThreadBusyError as e:
        logger.info("REST THREAD_BUSY sid=%s", session.sid)
        return JSONResponse(thread_busy_payload(e), status_code=409)
    except Exception as e:
        logger.error(f"REST 处理失败: {e}", exc_info=True)
        return JSONResponse({"error": ERR_INTERNAL}, status_code=500)


# ── SSE 流式输出 ──


@router.post("/api/chat/stream")
async def stream_chat(data: ChatStreamRequest, request: Request):
    """SSE 真流式输出端点（v4.2）"""
    try:
        session = await get_authenticated_session(
            request,
            data.session_id,
            data.session_token,
        )
    except _SessionValidationError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)

    query = sanitize_input(data.query)[:MAX_QUERY_LENGTH]
    if not query:
        return JSONResponse({"error": ERR_QUERY_EMPTY}, status_code=400)

    graph_task, chunk_queue = _build_sse_stream_context(
        request,
        session.sid,
        query,
        session.session_manager,
        session.client_provided_sid,
        user_id=session.user_id,
    )
    ctx = SSEStreamContext(
        graph_task=graph_task,
        chunk_queue=chunk_queue,
        sid=session.sid,
        session_manager=session.session_manager,
        client_provided_sid=session.client_provided_sid,
        status_msg="正在分析您的问题...",
        progress_msg="正在识别意图...",
    )

    return ManagedSSE(
        ctx,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 多模态图片对话 ──
