"""
告警管理路由（v4.0）
- GET  /api/alerts/config — 查看告警配置
- POST /api/alerts/test — 测试告警通知
- GET  /api/alerts/history — 告警历史
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from auth.router import require_admin, require_auth
from logger import get_logger

from .notifier import alert_notifier

logger = get_logger("alerts.router")

router = APIRouter(prefix="/api/alerts", tags=["告警"])


def require_supervisor_or_admin(request: Request):
    """要求管理员或主管权限"""
    user = require_auth(request)
    if user.role not in ("admin", "supervisor"):
        raise HTTPException(status_code=403, detail="需要管理员或主管权限")
    return user


class TestAlertRequest(BaseModel):
    title: str = Field(default="测试告警", max_length=100)
    content: str = Field(default="这是一条测试告警通知", max_length=500)
    severity: str = Field(default="info")


@router.get("/config")
async def get_alert_config(request: Request):
    """查看告警配置"""
    _ = require_supervisor_or_admin(request)
    return alert_notifier.get_config()


@router.post("/test")
async def test_alert(data: TestAlertRequest, request: Request):
    """测试发送告警"""
    _ = require_admin(request)
    await alert_notifier.send_alert(data.title, data.content, data.severity)
    return {
        "message": "测试告警已发送",
        "channels": len(alert_notifier.webhooks) + (1 if alert_notifier.email_enabled else 0),
    }


@router.get("/history")
async def alert_history(request: Request, limit: int = 20):
    """告警历史"""
    _ = require_supervisor_or_admin(request)
    return {"alerts": alert_notifier.get_history(limit)}
