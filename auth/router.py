"""
认证路由（v4.0 — JWT Bearer 认证）
- POST /api/auth/register — 注册
- POST /api/auth/login — 登录
- GET  /api/auth/me — 当前用户信息
- GET  /api/auth/users — 用户列表（admin）
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, Field, validator
from typing import Optional

from db.database import get_db
from db.models import User, AuditLog
from .service import register_user, authenticate_user, get_current_user, decode_token, revoke_token, refresh_access_token


def _dt_to_iso(dt):
    """将 datetime 对象转换为 ISO 格式字符串，None 安全"""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt
from logger import get_logger

logger = get_logger("auth.router")

router = APIRouter(prefix="/api/auth", tags=["认证"])


# ── 请求模型 ──

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(..., min_length=6, max_length=64)
    display_name: str = Field(default="", max_length=64)

    @validator("password")
    def password_complexity(cls, v):
        """v5.0: 要求至少包含两类字符（字母+数字、字母+特殊字符等）"""
        has_letter = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        has_special = any(not c.isalnum() for c in v)
        classes = sum([has_letter, has_digit, has_special])
        if classes < 2:
            raise ValueError("密码需包含至少两类字符（字母、数字、特殊字符）")
        return v


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=32)
    password: str = Field(..., max_length=64)


class RefreshRequest(BaseModel):
    """P2-3: Refresh Token 请求"""
    refresh_token: str


# ── 辅助函数 ──

def _get_token_from_request(request: Request) -> str:
    """从请求中提取 JWT token"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return ""


def get_current_user_from_request(request: Request) -> Optional[User]:
    """从请求获取当前用户"""
    token = _get_token_from_request(request)
    if not token:
        return None
    return get_current_user(token)


def require_auth(request: Request) -> User:
    """要求认证（FastAPI Depends 用）"""
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user


def require_admin(request: Request) -> User:
    """要求管理员权限"""
    user = require_auth(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ── 路由 ──

@router.post("/register")
async def api_register(data: RegisterRequest, request: Request, db=Depends(get_db)):
    """用户注册"""
    result = register_user(data.username, data.password, data.display_name)

    # 审计日志
    ip = request.client.host if request.client else "unknown"
    log = AuditLog(
        action="register",
        detail=f"username={data.username} success={result['success']}",
        ip_address=ip,
        timestamp=datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return {"message": "注册成功", "user_id": result["user_id"], "username": result["username"]}


@router.post("/login")
async def api_login(data: LoginRequest, request: Request, db=Depends(get_db)):
    """用户登录"""
    result = authenticate_user(data.username, data.password)

    ip = request.client.host if request.client else "unknown"
    log = AuditLog(
        action="login",
        detail=f"username={data.username} success={result is not None}",
        ip_address=ip,
        timestamp=datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()

    if not result:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return result


@router.post("/refresh")
async def api_refresh(data: RefreshRequest):
    """P2-3: 用 refresh_token 换取新的 access_token"""
    result = refresh_access_token(data.refresh_token)
    if not result:
        raise HTTPException(status_code=401, detail="refresh_token 无效或已过期")
    return result


@router.post("/logout")
async def api_logout(request: Request):
    """用户登出（v4.0: 吊销当前 JWT token）"""
    user = require_auth(request)
    token = _get_token_from_request(request)
    if token:
        revoke_token(token)
    return {"message": "已登出"}


@router.get("/me")
async def api_me(request: Request):
    """获取当前用户信息"""
    user = require_auth(request)
    return {
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        "display_name": user.display_name,
        "created_at": _dt_to_iso(user.created_at),
        "last_login_at": _dt_to_iso(user.last_login_at),
    }


@router.get("/users")
async def api_list_users(request: Request, db=Depends(get_db)):
    """用户列表（admin only）"""
    _ = require_admin(request)
    users = db.query(User).order_by(User.created_at.desc()).all()
    return {
        "users": [
            {
                "user_id": u.id,
                "username": u.username,
                "role": u.role,
                "display_name": u.display_name,
                "is_active": bool(u.is_active),
                "created_at": _dt_to_iso(u.created_at),
                "last_login_at": _dt_to_iso(u.last_login_at),
            }
            for u in users
        ]
    }


@router.get("/audit")
async def api_audit_log(request: Request, limit: int = 50, db=Depends(get_db)):
    """审计日志（admin only）"""
    _ = require_admin(request)
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(min(limit, 200)).all()
    return {
        "logs": [
            {
                "id": l.id,
                "user_id": l.user_id,
                "action": l.action,
                "detail": l.detail,
                "ip_address": l.ip_address,
                "timestamp": _dt_to_iso(l.timestamp),
            }
            for l in logs
        ]
    }


class UpdateRoleRequest(BaseModel):
    role: str

@router.put("/users/{user_id}/role")
async def update_user_role(user_id: int, req: UpdateRoleRequest, request: Request, admin_user=Depends(require_admin), db=Depends(get_db)):
    """管理员修改用户角色"""
    VALID_ROLES = {"customer", "agent", "supervisor", "admin"}
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"无效角色，可选: {', '.join(VALID_ROLES)}")
    from db.models import User
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.role = req.role
    db.commit()

    # 审计日志
    ip = request.client.host if request.client else "unknown"
    log = AuditLog(
        user_id=admin_user.id,
        action="update_user_role",
        detail=f"target_user={user_id} new_role={req.role}",
        ip_address=ip,
        timestamp=datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()

    return {"message": "角色已更新", "user_id": user_id, "role": req.role}
