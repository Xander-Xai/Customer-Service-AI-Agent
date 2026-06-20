"""
聊天相关路由：REST / SSE 流式 / 多模态图片 / 语音 / TTS / 文件上传
从 api/app.py create_app() 提取，通过 request.app.state 访问依赖。
"""

import asyncio
import json
import os  # noqa: F401 — 保留给 test_tts_voices_list mock 路径: api.routes.chat.os.getenv
import time
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from api.utils import extract_user_id, sanitize_input, validate_session_id
from core.config import MAX_QUERY_LENGTH
from core.logger import get_logger

router = APIRouter()
logger = get_logger("api.chat")

# ===== SSE 流式常量 =====
SSE_CHUNK_TIMEOUT = 60.0  # SSE 事件队列等待超时（秒）
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


async def get_authenticated_session(
    request: Request,
    session_id: str = "",
    session_token: str = "",
) -> AuthenticatedSession:
    """
    共享依赖：验证 session_id 格式、检查会话令牌、设置 user_id。
    所有需要会话的端点调用此函数，失败时抛出 _SessionValidationError。
    """
    state = request.app.state
    session_manager = state.session_manager

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
) -> tuple[asyncio.Task, asyncio.Queue]:
    """构建 SSE 流式任务和队列，返回 (graph_task, chunk_queue)。"""
    run_graph = request.app.state.run_graph
    chunk_queue: asyncio.Queue = asyncio.Queue()

    async def stream_callback(event: dict):
        await chunk_queue.put(event)

    kwargs = {"stream_callback": stream_callback}
    if multimodal_content is not None:
        kwargs["multimodal_content"] = multimodal_content
    graph_task = asyncio.create_task(run_graph(sid, query, **kwargs))
    return graph_task, chunk_queue


async def _sse_stream_generator(ctx: SSEStreamContext):
    """共享的 SSE 事件生成器，供 stream_chat 和 stream_multimodal_chat 复用。"""
    start_time = time.time()
    try:
        yield _sse_event({"type": "status", "content": ctx.status_msg})
        yield _sse_event({"type": "progress", "content": ctx.progress_msg})

        if not ctx.graph_task.done():
            while True:
                try:
                    event = await asyncio.wait_for(ctx.chunk_queue.get(), timeout=SSE_CHUNK_TIMEOUT)
                except asyncio.TimeoutError:
                    if ctx.graph_task.done():
                        break
                    continue
                if event is None:
                    break
                if isinstance(event, dict) and event.get("type") in (
                    "chunk", "status", "thinking", "tool_call",
                    "tool_result", "rag_status", "agent_switch",
                ):
                    yield _sse_event(event)

        try:
            result = await ctx.graph_task
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
                "session_id": ctx.sid,
                "session_token": session_token,
            }
        )
    except Exception as e:
        logger.error(f"SSE 流式处理失败: {e}", exc_info=True)
        yield _sse_event({"type": "error", "content": ERR_INTERNAL})


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
        user_id = extract_user_id(request)
        result = await run_graph(session.sid, query, user_id=user_id)
        # 统一字段映射（对齐 WS/SSE 端点）
        mapped = {
            "response": result.get("response", ""),
            "agent": result.get("current_agent", ""),
            "mode": result.get("collaboration_mode", "sequential"),
            "elapsed": result.get("elapsed", 0),
            "cached": result.get("cached", False),
            "agents_used": result.get("agents_used", []),
            "resolution_status": result.get("resolution_status", ""),
            "session_id": session.sid,
        }
        if session.session_manager:
            mapped["session_token"] = session.session_manager.generate_session_token(session.sid)
        return mapped
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

    return StreamingResponse(
        _sse_stream_generator(ctx),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 多模态图片对话 ──
