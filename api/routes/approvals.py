"""高风险操作人工审批 API（RBAC）。

GET  /api/approvals?status=PENDING   -> 待审批列表（supervisor/admin）
GET  /api/approvals/{approval_id}     -> 审批详情
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject

普通 customer 不能审批；且不能审批自己发起的操作（reviewer != requester）。
审批通过后 run 从 WAITING_APPROVAL 恢复执行；approve 仍经 Tool idempotency。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.hitl.approval_service import (
    ApprovalNotFound,
    ApprovalService,
    InvalidApprovalDecision,
    get_approval_service,
)
from core.logger import get_logger
from runtime.run_service import RunService
from runtime.statuses import InvalidRunTransition

router = APIRouter()
logger = get_logger("api.approvals")

_REVIEWER_ROLES = ("admin", "supervisor")


class DecisionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    edited_args: dict | None = None


def _approval_service(request: Request) -> ApprovalService:
    service = getattr(request.app.state, "approval_service", None)
    return service or get_approval_service()


def _run_service(request: Request) -> RunService:
    service = getattr(request.app.state, "run_service", None)
    if service is not None:
        return service
    from runtime.run_service import get_run_service

    return get_run_service()


def _require_reviewer(request: Request):
    """审批权限：admin token 或 supervisor/admin JWT。"""
    from api.utils import check_admin_token

    if check_admin_token(request):
        return None
    from auth.router import require_auth

    user = require_auth(request)
    if getattr(user, "role", "") not in _REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="需要主管或管理员权限")
    return user


def _reviewer_id(request: Request, reviewer) -> str:
    if reviewer is None:
        return "admin-token"
    return str(
        getattr(reviewer, "username", None)
        or getattr(reviewer, "id", None)
        or "reviewer"
    )


def _serialize(record: dict) -> dict:
    def _iso(value):
        return value.isoformat() if value else None

    return {
        "approval_id": record["approval_id"],
        "run_id": record["run_id"],
        "thread_id": record["thread_id"],
        "action": record["action"],
        "risk_level": record["risk_level"],
        "agent": record.get("agent"),
        "proposal": record["proposal"],
        "status": record["status"],
        "requested_at": _iso(record["requested_at"]),
        "reviewed_at": _iso(record.get("reviewed_at")),
        "reviewer_id": record.get("reviewer_id"),
        "decision": record.get("decision"),
        "reason": record.get("reason"),
    }


async def _resume_run_if_waiting(request: Request, run_id: str) -> None:
    run_service = _run_service(request)
    try:
        run_service.mark_resumed(run_id)
    except InvalidRunTransition:
        return  # 已恢复/已终态：幂等
    from runtime import dispatch

    try:
        await dispatch.dispatch_run(run_id)
    except Exception as e:
        logger.error("审批恢复入队失败 run=%s: %s", run_id, type(e).__name__)


@router.get("/api/approvals")
async def list_approvals(request: Request, status: str = "PENDING", limit: int = 100):
    _require_reviewer(request)
    service = _approval_service(request)
    limit = min(max(limit, 1), 500)
    records = service.list_pending(limit=limit)
    return {"approvals": [_serialize(r) for r in records], "count": len(records)}


@router.get("/api/approvals/{approval_id}")
async def get_approval(approval_id: str, request: Request):
    _require_reviewer(request)
    try:
        record = _approval_service(request).require(approval_id)
    except ApprovalNotFound:
        return JSONResponse({"error": "审批不存在"}, status_code=404)
    return _serialize(record)


@router.post("/api/approvals/{approval_id}/approve")
async def approve(approval_id: str, request: Request, body: DecisionRequest | None = None):
    reviewer = _require_reviewer(request)
    reviewer_id = _reviewer_id(request, reviewer)
    service = _approval_service(request)
    try:
        record = service.require(approval_id)
    except ApprovalNotFound:
        return JSONResponse({"error": "审批不存在"}, status_code=404)

    # 不能审批自己发起的操作
    run = _run_service(request).get_run(record["run_id"])
    if run and run.get("user_id") and str(run["user_id"]) == reviewer_id:
        raise HTTPException(status_code=403, detail="不能审批自己发起的操作")

    body = body or DecisionRequest()
    try:
        record, newly = service.decide(
            approval_id,
            reviewer_id=reviewer_id,
            decision="approve",
            reason=body.reason,
            edited_args=body.edited_args,
        )
    except InvalidApprovalDecision as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if newly:
        await _resume_run_if_waiting(request, record["run_id"])
    return _serialize(record)


@router.post("/api/approvals/{approval_id}/reject")
async def reject(approval_id: str, request: Request, body: DecisionRequest | None = None):
    reviewer = _require_reviewer(request)
    reviewer_id = _reviewer_id(request, reviewer)
    service = _approval_service(request)
    try:
        record = service.require(approval_id)
    except ApprovalNotFound:
        return JSONResponse({"error": "审批不存在"}, status_code=404)

    run = _run_service(request).get_run(record["run_id"])
    if run and run.get("user_id") and str(run["user_id"]) == reviewer_id:
        raise HTTPException(status_code=403, detail="不能审批自己发起的操作")

    body = body or DecisionRequest()
    try:
        record, newly = service.decide(
            approval_id,
            reviewer_id=reviewer_id,
            decision="reject",
            reason=body.reason,
        )
    except InvalidApprovalDecision as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if newly:
        await _resume_run_if_waiting(request, record["run_id"])
    return _serialize(record)
