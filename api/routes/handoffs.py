"""Human support ticket API. Human handoff is NOT the HITL refund approval API.

Customers can create/view their own session ticket; agents claim an open ticket
atomically; staff replies are persisted and available to the customer by polling.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.handoff_service import (
    HandoffConflict,
    HandoffForbidden,
    HandoffNotFound,
    get_handoff_service,
)

router = APIRouter(prefix="/api/handoffs", tags=["human-handoff"])


class CreateHandoffRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    session_token: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=2000)
    message: str = Field(default="", max_length=4096)


class SendMessageRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=4096)
    client_message_id: str = Field(..., min_length=1, max_length=64)


class CloseHandoffRequest(BaseModel):
    resolution: str = Field(..., min_length=1, max_length=2000)


def _identity(request: Request) -> tuple[str, str]:
    from auth.router import require_auth

    user = require_auth(request)
    user_id = str(getattr(user, "id", "") or "")
    role = str(getattr(user, "role", "") or "").lower()
    if not user_id or role not in {"customer", "agent", "supervisor", "admin"}:
        raise HTTPException(status_code=403, detail="需要有效用户身份")
    return user_id, role


def _service():
    return get_handoff_service()


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, HandoffNotFound):
        raise HTTPException(status_code=404, detail="工单不存在或无权访问") from exc
    if isinstance(exc, HandoffForbidden):
        raise HTTPException(status_code=403, detail="无权执行此操作") from exc
    if isinstance(exc, HandoffConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.post("")
async def create_handoff(data: CreateHandoffRequest, request: Request):
    actor, role = _identity(request)
    if role != "customer":
        raise HTTPException(status_code=403, detail="仅客户可申请转人工")
    # A customer must prove possession of the session token, not merely guess a
    # session ID. JWT identity alone does not grant access to another session.
    from api.routes.chat import _SessionValidationError, get_authenticated_session

    sm = getattr(request.app.state, "session_manager", None)
    if sm is None:
        raise HTTPException(status_code=503, detail="会话服务不可用")
    try:
        session = await get_authenticated_session(
            request, data.session_id, data.session_token
        )
    except _SessionValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if session.sid != data.session_id or session.user_id != actor:
        raise HTTPException(status_code=403, detail="会话身份校验失败")
    try:
        return _service().create_or_get(
            session_id=data.session_id,
            customer_id=actor,
            reason=data.reason,
            source="customer",
            initial_message=data.message or data.reason,
        )
    except Exception as exc:
        _handle_error(exc)


@router.get("")
async def list_queue(request: Request, status: str | None = None, limit: int = 50):
    actor, role = _identity(request)
    try:
        return {"tickets": _service().queue(actor_id=actor, role=role, status=status, limit=limit)}
    except Exception as exc:
        _handle_error(exc)


@router.get("/session/{session_id}")
async def get_session_handoff(session_id: str, request: Request):
    actor, role = _identity(request)
    if role != "customer":
        raise HTTPException(status_code=403, detail="仅客户可按会话查询工单")
    try:
        ticket = _service().by_session(session_id, actor_id=actor, role=role)
        if ticket is None:
            raise HandoffNotFound("Ticket not found")
        return ticket
    except Exception as exc:
        _handle_error(exc)


@router.get("/{ticket_id}")
async def get_handoff(ticket_id: str, request: Request):
    actor, role = _identity(request)
    try:
        return _service().get(ticket_id, actor_id=actor, role=role)
    except Exception as exc:
        _handle_error(exc)


@router.post("/{ticket_id}/claim")
async def claim_handoff(ticket_id: str, request: Request):
    actor, role = _identity(request)
    try:
        return _service().claim(ticket_id, actor_id=actor, role=role)
    except Exception as exc:
        _handle_error(exc)


@router.get("/{ticket_id}/messages")
async def list_messages(ticket_id: str, request: Request, limit: int = 100):
    actor, role = _identity(request)
    try:
        return {
            "messages": _service().messages(
                ticket_id, actor_id=actor, role=role, limit=limit
            )
        }
    except Exception as exc:
        _handle_error(exc)


@router.post("/{ticket_id}/messages")
async def send_message(ticket_id: str, data: SendMessageRequest, request: Request):
    actor, role = _identity(request)
    try:
        return _service().post_message(
            ticket_id,
            actor_id=actor,
            role=role,
            body=data.body,
            client_message_id=data.client_message_id,
        )
    except Exception as exc:
        _handle_error(exc)


@router.get("/{ticket_id}/events")
async def list_events(ticket_id: str, request: Request):
    actor, role = _identity(request)
    try:
        return {"events": _service().events(ticket_id, actor_id=actor, role=role)}
    except Exception as exc:
        _handle_error(exc)


@router.post("/{ticket_id}/close")
async def close_handoff(ticket_id: str, data: CloseHandoffRequest, request: Request):
    actor, role = _identity(request)
    try:
        return _service().close(
            ticket_id, actor_id=actor, role=role, resolution=data.resolution
        )
    except Exception as exc:
        _handle_error(exc)


@router.post("/{ticket_id}/reopen")
async def reopen_handoff(ticket_id: str, request: Request):
    actor, role = _identity(request)
    try:
        return _service().reopen(ticket_id, actor_id=actor, role=role)
    except Exception as exc:
        _handle_error(exc)
