"""人工审批 API（human-in-the-loop）。

端点：

    GET  /api/approvals?status=PENDING   待审批队列（supervisor/admin）
    GET  /api/approvals/{id}             单条审批详情（supervisor/admin）
    POST /api/approvals/{id}/decision    approve | edit | reject（supervisor/admin）
    GET  /api/approvals/by-run/{run_id}  某 run 的全部审批（supervisor/admin）

安全边界（三层，缺一不可）
------------------------
1. **RBAC**：只有 ``admin`` / ``supervisor`` 可读可决策；``customer`` / ``agent``
   永远没有审批权。复用 ``api.routes.monitoring._require_monitoring_auth`` 的
   既有鉴权形态（admin token 或高级角色 JWT），不另造一套。
2. **职责分离**（reviewer != requester）：在 **service 层**
   （``ApprovalService._guard_reviewer``）强制，API 层只是**额外**的一道。本层
   刻意**不用**「查 run 的 user_id 再比对」这种间接判断——run 查不到就等于没
   检查，是 fail-open。这里直接用审批记录自身的 ``user_id`` 比对。
3. **审批人标识不可伪造为空**：``_reviewer_id`` 拿不到身份时抛 401，绝不
   退化成 ``"admin-token"`` 之类的固定串——否则所有 admin-token 决策会共享
   同一个 reviewer_id，职责分离与审计都失去意义。

决策后的恢复
------------
``POST .../decision`` 只负责记录决策并**请求恢复**（dispatch）。它**不**把 run
改回 ``QUEUED``：``WAITING_APPROVAL -> RUNNING`` 由 worker 领取时经
``RunService.mark_resumed_running`` 原子迁移且**不递增 attempt**（等待人不是
失败，不该消耗 ``AGENT_RUN_MAX_ATTEMPTS``）。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from core.hitl.approval_service import (
    ALLOWED_DECISIONS,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalService,
    InvalidApprovalDecision,
    SelfApprovalForbidden,
    get_approval_service,
    is_expired,
)
from core.logger import get_logger

router = APIRouter(prefix="/api/approvals", tags=["approvals"])
logger = get_logger("api.approvals")

#: 可审批的高级角色。customer / agent 永不在列。
#:
#: 唯一真相源是 ``core/config.py::HITL_REVIEWER_ROLES``（env ``HITL_REVIEWER_ROLES``，
#: 默认 ``admin,supervisor``）。这里**不**再硬编码第二份列表——一个只写在
#: .env.example / 文档里、代码从不读取的旋钮就是配置漂移：运维改了 env 却没有任何
#: 效果，而文档声称它可配。
def _reviewer_roles() -> tuple[str, ...]:
    from core.config import HITL_REVIEWER_ROLES

    return tuple(HITL_REVIEWER_ROLES)


#: 模块级快照，保留原有可导入符号（``from ... import REVIEWER_ROLES``）。
REVIEWER_ROLES = _reviewer_roles()


def _require_reviewer(request: Request):
    """RBAC：只有 admin / supervisor 可读可决策；其他一律 401/403。

    这里**不复用** ``api.routes.monitoring._require_monitoring_auth``：那是监控端点的
    私有实现（单下划线），且被测试用模块级赋值替换过。审批是治理边界，它的准入规则
    应当在本模块内可独立审阅，而不是间接依赖另一个路由模块的私有符号。
    底层仍用同一套公开认证原语（admin token / JWT），因此与全系统认证语义一致。
    """
    from api.utils import check_admin_token

    if check_admin_token(request):
        return None  # 系统间调用（监控/自动化），无 user 对象

    from auth.router import require_auth

    user = require_auth(request)  # 未认证 -> 401
    role = str(getattr(user, "role", "") or "").lower()
    if role not in _reviewer_roles():
        raise HTTPException(
            status_code=403,
            detail=(
                "需要管理员或主管权限"
                f"（可审批角色：{' / '.join(_reviewer_roles())}）"
            ),
        )
    return user


def _reviewer_id(request: Request, reviewer) -> str:
    """解析审批人标识。**拿不到就 401**，绝不退化成固定串。

    这是职责分离与审计的前提：若所有 admin-token 决策都记成同一个
    ``admin-token``，则 (a) 无法分辨谁批的，(b) 与发起人同名的判断会失真。
    """
    identity = None
    if reviewer is not None:
        for attr in ("username", "id", "user_id", "email"):
            value = getattr(reviewer, attr, None)
            if value:
                identity = str(value)
                break
    if not identity:
        # admin token 路径没有 user 对象：要求显式带审批人身份头。
        identity = request.headers.get("X-Reviewer-Id")
    if not identity or not str(identity).strip():
        raise HTTPException(
            status_code=401,
            detail="无法确定审批人身份：请使用 JWT，或提供 X-Reviewer-Id 头",
        )
    return str(identity).strip()


def _service() -> ApprovalService:
    return get_approval_service()


async def _dispatch_resume(run_id: str) -> bool:
    """请求 worker 恢复该 run。失败**不**回滚已记录的决策。

    ``dispatch_run`` 是协程，必须 await：未 await 的 coroutine 不会入队，审批通过
    的 run 会永远停在 ``WAITING_APPROVAL``（已决策但没人执行）。

    投递失败时决策仍是事实：run 停在 ``WAITING_APPROVAL``，可由运维重投（或
    ``reconcile_stuck_runs``）。审批记录不会因此丢失或重复执行——真正执行仍受
    side-effect ledger 的 ``operation_key`` 保护。
    """
    try:
        from runtime.dispatch import dispatch_run

        await dispatch_run(run_id)
        return True
    except Exception as e:
        logger.error(
            "审批已决策但恢复投递失败 run_id=%s err=%s（可人工重投）",
            run_id,
            type(e).__name__,
        )
        return False


def _serialize(record: dict) -> dict:
    """响应体。显式白名单：不要把内部字段整行倒出去。"""
    return {
        "approval_id": record.get("approval_id"),
        "run_id": record.get("run_id"),
        "thread_id": record.get("thread_id"),
        "user_id": record.get("user_id"),
        "action": record.get("action"),
        "risk_level": record.get("risk_level"),
        "agent": record.get("agent"),
        "proposal": record.get("proposal"),
        "status": record.get("status"),
        "requested_at": record.get("requested_at"),
        "expires_at": record.get("expires_at"),
        "expired": is_expired(record),
        "reviewed_at": record.get("reviewed_at"),
        "reviewer_id": record.get("reviewer_id"),
        "decision": record.get("decision"),
        "reason": record.get("reason"),
        "resumed_at": record.get("resumed_at"),
    }


@router.get("")
async def list_approvals(
    request: Request,
    status: str = "PENDING",
    limit: int = 100,
):
    """审批队列。``status=PENDING`` 时顺带把已超时项收敛成 ``EXPIRED``。"""
    _require_reviewer(request)
    svc = _service()
    normalized = (status or "PENDING").strip().upper()
    if normalized == "PENDING":
        records = svc.list_pending(limit=min(limit, 500))
    else:
        records = svc.list_by_status(normalized, limit=min(limit, 500))
    return {"total": len(records), "items": [_serialize(r) for r in records]}


@router.get("/by-run/{run_id}")
async def list_approvals_by_run(run_id: str, request: Request):
    _require_reviewer(request)
    records = _service().list_by_run(run_id)
    return {"run_id": run_id, "total": len(records), "items": [_serialize(r) for r in records]}


@router.get("/{approval_id}")
async def get_approval(approval_id: str, request: Request):
    _require_reviewer(request)
    try:
        record = _service().require(approval_id)
    except ApprovalNotFound as e:
        raise HTTPException(status_code=404, detail="审批不存在") from e
    return _serialize(record)


@router.post("/{approval_id}/decision")
async def decide_approval(
    approval_id: str,
    request: Request,
    payload: dict = Body(...),
):
    """记录人工决策：``approve`` | ``edit`` | ``reject``。

    - ``edit`` 必须带 ``edited_args``（改参数后放行）；
    - ``reject`` 可带 ``reason``；
    - 幂等：重复提交返回既有结果，``newly_decided=False``；
    - 已过期（TTL）返回 409，**不会**被当作批准。
    """
    reviewer = _require_reviewer(request)
    reviewer_id = _reviewer_id(request, reviewer)

    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in ALLOWED_DECISIONS:
        raise HTTPException(
            status_code=400,
            detail=f"非法决策: {decision!r}（允许 {sorted(ALLOWED_DECISIONS)}）",
        )

    svc = _service()
    try:
        record, newly_decided = svc.decide(
            approval_id,
            reviewer_id=reviewer_id,
            decision=decision,
            reason=payload.get("reason"),
            edited_args=payload.get("edited_args"),
        )
    except ApprovalNotFound as e:
        raise HTTPException(status_code=404, detail="审批不存在") from e
    except SelfApprovalForbidden as e:
        # 职责分离是硬边界：403 且不泄露更多细节。
        logger.warning("自审被拒 approval=%s reviewer=%s", approval_id, reviewer_id)
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ApprovalExpired as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidApprovalDecision as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    dispatched = False
    if newly_decided:
        dispatched = await _dispatch_resume(record["run_id"])
        logger.info(
            "审批已决策 approval=%s run=%s action=%s decision=%s dispatched=%s",
            approval_id,
            record.get("run_id"),
            record.get("action"),
            decision,
            dispatched,
        )

    return {
        "approval": _serialize(record),
        "newly_decided": newly_decided,
        "resume_dispatched": dispatched,
    }


__all__ = ["REVIEWER_ROLES", "router"]
