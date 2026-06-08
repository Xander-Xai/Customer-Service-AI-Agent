"""
会话相关路由：会话 CRUD、历史、消息查询
从 api/app.py create_app() 提取。
"""
import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from logger import get_logger

router = APIRouter()
logger = get_logger("api.sessions")


def _extract_user_id(request: Request) -> str | None:
    """从 JWT payload 或 API Key 认证结果中提取 user_id"""
    state = request.app.state
    payload = getattr(state, "_current_jwt_payload", None) or getattr(request.state, "jwt_payload", None)
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
        return True
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
    if not dev_mode:
        token = request.headers.get("X-Session-Token", "")
        if not sm.validate_session_token(session_id, token):
            return JSONResponse({"error": "会话令牌无效或无权访问"}, status_code=403)
    session = await sm.get_session(session_id)
    if session:
        user_id = _extract_user_id(request)
        if not _check_session_ownership(session, user_id, dev_mode):
            return JSONResponse({"error": "无权访问该会话"}, status_code=403)
        return {"session": {
            "session_id": session_id,
            "messages": session.get("messages", []),
            "created_at": session.get("created_at"),
            "last_activity": session.get("last_activity"),
            "message_count": session.get("message_count", 0),
            "summary": session.get("summary", ""),
        }}
    return JSONResponse({"error": "session not found"}, status_code=404)


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
    return {"message": f"会话 {session_id} 已删除"}


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
    return {"messages": [
        {"role": m.get("role", ""), "content": m.get("content", ""), "timestamp": m.get("timestamp", 0)}
        for m in messages
    ]}
