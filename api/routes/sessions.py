"""
会话相关路由：会话 CRUD、历史、消息查询
从 api/app.py create_app() 提取。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.logger import get_logger

router = APIRouter()
logger = get_logger("api.sessions")


def extract_checkpoint_id(checkpoint: object) -> str | None:
    """从 checkpoint 结构里取 id，兼容两种形态。

    官方 ``AsyncPostgresSaver`` 的 ``aget_tuple()`` 返回的 ``checkpoint`` 是
    **mapping**（含 ``"id"`` 键），没有 ``.id`` 属性；只有自定义 saver 才可能返回带
    ``.id`` 的对象。此前只做 ``hasattr(checkpoint, "id")``，导致真实 PostgreSQL
    checkpoint 永远被报成 ``checkpoint_id: null``（has_checkpoint 却为 true）。

    顺序：真 mapping 取 ``"id"`` -> 对象取 ``.id`` -> mapping 取 ``"checkpoint_id"``。
    刻意用 ``isinstance(..., Mapping)`` 而不是鸭子类型的 ``hasattr(x, "get")``：
    后者对 MagicMock 之类"任何方法都存在"的对象会返回伪造值。
    取不到时返回 ``None``（不猜测、不伪造 id）。
    """
    from collections.abc import Mapping

    if checkpoint is None:
        return None
    if isinstance(checkpoint, Mapping):
        for key in ("id", "checkpoint_id"):
            value = checkpoint.get(key)
            if value not in (None, ""):
                return str(value)
        return None
    value = getattr(checkpoint, "id", None)
    if value not in (None, ""):
        return str(value)
    return None


def _extract_user_id(request: Request) -> str | None:
    """从 JWT payload 或 API Key 认证结果中提取 user_id"""
    state = request.app.state
    payload = getattr(state, "_current_jwt_payload", None) or getattr(
        request.state, "jwt_payload", None
    )
    if payload:
        return str(payload.get("sub", ""))
    return None


def _check_session_ownership(session: dict, user_id: str | None, dev_mode: bool) -> bool:
    """检查会话是否属于当前用户"""
    if dev_mode:
        return True
    if not user_id:
        return False
    session_user = session.get("user_id")
    if not session_user:
        # 会话无归属用户 - 仅允许当前用户在非DEV_MODE下访问
        # 避免未认证状态下创建的会话被任意已认证用户看到
        return True  # 向后兼容：匿名会话对所有认证用户可见
    return session_user == user_id


@router.get("/api/sessions")
async def list_sessions(request: Request, offset: int = 0, limit: int = 20):
    state = request.app.state
    sm = getattr(state, "session_manager", None)
    dev_mode = getattr(state, "dev_mode", False)
    if sm:
        offset = max(offset, 0)
        limit = min(max(limit, 1), 100)
        result = await sm.list_sessions_brief(offset=offset, limit=limit)
        user_id = _extract_user_id(request)
        if user_id and not dev_mode:
            filtered = [s for s in result.get("sessions", []) if s.get("user_id") == user_id]
            result["sessions"] = filtered
            result["total"] = len(filtered)
        return result
    return {"sessions": [], "total": 0, "offset": offset, "limit": limit}


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    state = request.app.state
    sm = getattr(state, "session_manager", None)
    dev_mode = getattr(state, "dev_mode", False)
    if not sm:
        return JSONResponse({"error": "session manager not initialized"}, status_code=500)
    session = await sm.get_session(session_id)
    if session:
        user_id = _extract_user_id(request)
        if not _check_session_ownership(session, user_id, dev_mode):
            return JSONResponse({"error": "无权访问该会话"}, status_code=403)
        # 返回 session_token，使前端切换会话后能继续操作
        session_token = sm.generate_session_token(session_id) if not dev_mode else ""
        return {
            "session": {
                "session_id": session_id,
                "messages": session.get("messages", []),
                "created_at": session.get("created_at"),
                "last_activity": session.get("last_activity"),
                "message_count": session.get("message_count", 0),
                "summary": session.get("summary", ""),
                "session_token": session_token,
            }
        }
    return JSONResponse({"error": "session not found"}, status_code=404)


async def delete_session_checkpoint(request: Request, session_id: str) -> bool:
    """删除该会话对应的 LangGraph checkpoint thread。返回是否真的删了。

    thread_id == session_id。兼容三种 saver：
      - 官方 Async/PostgresSaver：``adelete_thread``
      - MemorySaver（同步）：``delete_thread``
      - 无删除能力：返回 False（调用方仍应删除 Session，只是不静默谎称已清理）

    永不抛错：checkpoint 清理失败不能把"会话已删除"变成 500 —— Session 已经删掉了，
    残留的 checkpoint 由 retention 任务兜底。
    """
    state = request.app.state
    graph_app = getattr(state, "graph_app", None)
    checkpointer = getattr(graph_app, "checkpointer", None) if graph_app else None
    if not checkpointer:
        return False

    config = {"configurable": {"thread_id": session_id}}
    for name in ("adelete_thread", "delete_thread"):
        fn = getattr(checkpointer, name, None)
        if fn is None:
            continue
        try:
            import inspect

            result = fn(config)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as e:
            logger.warning(
                "checkpoint thread 删除失败 session=%s saver=%s err=%s",
                session_id,
                name,
                type(e).__name__,
            )
            return False
    logger.debug("checkpoint saver 不支持删除 thread（session=%s）", session_id)
    return False


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    state = request.app.state
    sm = getattr(state, "session_manager", None)
    dev_mode = getattr(state, "dev_mode", False)
    if not sm:
        return JSONResponse({"error": "session manager not initialized"}, status_code=500)
    user_id = _extract_user_id(request)
    session = await sm.get_session(session_id)
    if session and not _check_session_ownership(session, user_id, dev_mode):
        return JSONResponse({"error": "无权删除该会话"}, status_code=403)
    if not dev_mode:
        token = request.headers.get("X-Session-Token", "")
        if not sm.validate_session_token(session_id, token):
            return JSONResponse({"error": "会话令牌无效或无权删除"}, status_code=403)
    await sm.delete_session(session_id)
    # 图状态（LangGraph checkpoint）也是该会话的持久数据，必须与 Session 一起删除。
    # 否则"删除会话"只清了 SessionManager/Redis，图状态仍留在 checkpoints 表里，
    # 复用同一 session_id 会把本应删除的对话历史合并回新请求。
    deleted_checkpoint = await delete_session_checkpoint(request, session_id)
    return {
        "message": f"会话 {session_id} 已删除",
        "checkpoint_deleted": deleted_checkpoint,
    }


@router.get("/api/sessions/{session_id}/checkpoint")
async def get_session_checkpoint(session_id: str, request: Request):
    """获取会话的 LangGraph checkpoint 状态（断点续传）。

    thread_id == session_id：检查同一会话对应的图状态快照是否存在。
    MemorySaver 走同步 ``get()``；官方 AsyncPostgresSaver 只实现异步接口，
    因此优先使用 ``aget_tuple()``（MagicMock 场景自动回退到同步 get）。
    访问控制与其它会话端点一致：非 dev 模式下校验会话归属。
    """
    import inspect

    from api.utils import sanitize_input

    clean_id = sanitize_input(session_id)
    if not clean_id:
        return JSONResponse({"error": "无效的 session_id"}, status_code=400)

    state = request.app.state

    # v5.1: 从 app.state 获取 graph_app，替代模块级全局变量 import
    graph_app = getattr(state, "graph_app", None)
    if not graph_app or not hasattr(graph_app, "checkpointer") or not graph_app.checkpointer:
        return JSONResponse({"error": "Checkpoint 功能未启用"}, status_code=503)

    # 会话归属校验（与 /api/sessions/{id} 一致，避免跨用户探测 checkpoint）
    sm = getattr(state, "session_manager", None)
    dev_mode = getattr(state, "dev_mode", False)
    if sm is not None:
        session = await sm.get_session(clean_id)
        user_id = _extract_user_id(request)
        if session and not _check_session_ownership(session, user_id, dev_mode):
            return JSONResponse({"error": "无权访问该会话"}, status_code=403)

    checkpointer = graph_app.checkpointer
    config = {"configurable": {"thread_id": clean_id}}
    try:
        aget = getattr(checkpointer, "aget_tuple", None)
        if aget is not None and inspect.iscoroutinefunction(aget):
            result = await aget(config)
            checkpoint = getattr(result, "checkpoint", result) if result else None
        else:
            checkpoint = checkpointer.get(config)
    except Exception as e:
        return JSONResponse({"error": f"查询 checkpoint 失败: {e}"}, status_code=500)

    if checkpoint:
        return {
            "session_id": clean_id,
            "has_checkpoint": True,
            "checkpoint_id": extract_checkpoint_id(checkpoint),
        }
    return {"session_id": clean_id, "has_checkpoint": False}


@router.get("/api/history")
async def list_history(request: Request, offset: int = 0, limit: int = 20):
    state = request.app.state
    sm = getattr(state, "session_manager", None)
    dev_mode = getattr(state, "dev_mode", False)
    if not sm:
        return {"sessions": [], "total": 0, "offset": offset, "limit": limit}
    offset = max(offset, 0)
    limit = min(max(limit, 1), 100)
    result = await sm.list_sessions_brief(offset=offset, limit=limit)
    user_id = _extract_user_id(request)
    if user_id and not dev_mode:
        filtered = [s for s in result.get("sessions", []) if s.get("user_id") == user_id]
        result["sessions"] = filtered
        result["total"] = len(filtered)
    return result


@router.get("/api/history/{session_id}/messages")
async def get_history_messages(session_id: str, request: Request):
    state = request.app.state
    sm = getattr(state, "session_manager", None)
    if not sm:
        return JSONResponse({"error": "session manager not initialized"}, status_code=500)
    user_id = _extract_user_id(request)
    session = await sm.get_session(session_id)
    dev_mode = getattr(state, "dev_mode", False)
    if session and not _check_session_ownership(session, user_id, dev_mode):
        return JSONResponse({"error": "无权访问该会话"}, status_code=403)
    token = request.headers.get("X-Session-Token", "")
    if not sm.validate_session_token(session_id, token):
        return JSONResponse({"error": "会话令牌无效或无权访问"}, status_code=403)
    if not session:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    messages = session.get("messages", [])
    return {
        "messages": [
            {
                "role": m.get("role", ""),
                "content": m.get("content", ""),
                "timestamp": m.get("timestamp", 0),
            }
            for m in messages
        ]
    }
