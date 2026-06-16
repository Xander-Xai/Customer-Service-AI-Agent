"""
Prompt 版本管理 API（v5.1）
提供 Prompt 的 CRUD、激活切换、版本对比、效果追踪。

端点：
- GET  /api/admin/prompts/agents  — 列出有 Prompt 版本的 Agent
- GET  /api/admin/prompts/{agent_name}        — 列出某 Agent 的所有版本
- POST /api/admin/prompts/{agent_name}        — 创建新版本
- PUT  /api/admin/prompts/{agent_name}/activate — 激活指定版本
- GET  /api/admin/prompts/{agent_name}/active  — 获取当前活跃版本
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.logger import get_logger

router = APIRouter()
logger = get_logger("api.prompts")


def _get_prompt_manager(request: Request):
    """从 app.state 或 global 获取 PromptManager"""
    # 显式检查属性是否被设置（区分 "设为 None" 和 "未设置"）
    if hasattr(request.app.state, "prompt_manager"):
        return request.app.state.prompt_manager
    from core.prompt_manager import get_prompt_manager

    return get_prompt_manager()


class PromptCreateRequest(BaseModel):
    """创建 Prompt 版本请求"""

    prompt_text: str = Field(..., min_length=10, max_length=10000)
    version: str = Field(..., min_length=1, max_length=20)
    activate: bool = Field(default=True)


class PromptActivateRequest(BaseModel):
    """激活 Prompt 版本请求"""

    version: str = Field(..., min_length=1, max_length=20)


# ===== 端点 =====


@router.get("/api/admin/prompts/agents")
async def list_agents(request: Request):
    """列出所有有 Prompt 版本的 Agent"""
    pm = _get_prompt_manager(request)
    if not pm:
        return JSONResponse({"error": "PromptManager 未初始化"}, status_code=503)
    agents = pm.list_agents()
    return {"agents": agents, "total": len(agents)}


@router.get("/api/admin/prompts/{agent_name}")
async def list_versions(request: Request, agent_name: str):
    """列出指定 Agent 的所有 Prompt 版本"""
    pm = _get_prompt_manager(request)
    if not pm:
        return JSONResponse({"error": "PromptManager 未初始化"}, status_code=503)
    versions = pm.list_versions(agent_name)
    return {"agent_name": agent_name, "versions": versions, "total": len(versions)}


@router.post("/api/admin/prompts/{agent_name}")
async def create_version(request: Request, agent_name: str, body: PromptCreateRequest):
    """创建新的 Prompt 版本"""
    pm = _get_prompt_manager(request)
    if not pm:
        return JSONResponse({"error": "PromptManager 未初始化"}, status_code=503)
    try:
        result = pm.save_prompt(
            agent_name=agent_name,
            prompt_text=body.prompt_text,
            version=body.version,
            activate=body.activate,
        )
        return {"status": "ok", "prompt": result}
    except Exception as e:
        logger.error(f"创建 Prompt 版本失败: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/api/admin/prompts/{agent_name}/activate")
async def activate_version(request: Request, agent_name: str, body: PromptActivateRequest):
    """激活指定版本（停用其他版本）"""
    pm = _get_prompt_manager(request)
    if not pm:
        return JSONResponse({"error": "PromptManager 未初始化"}, status_code=503)

    from db.database import get_db_session
    from db.models import PromptVersion

    db = get_db_session()
    try:
        # 停用所有版本
        db.query(PromptVersion).filter(
            PromptVersion.agent_name == agent_name,
        ).update({"is_active": 0})

        # 激活目标版本
        target = (
            db.query(PromptVersion)
            .filter(
                PromptVersion.agent_name == agent_name,
                PromptVersion.version == body.version,
            )
            .first()
        )
        if not target:
            db.rollback()
            return JSONResponse({"error": f"版本 {body.version} 不存在"}, status_code=404)

        target.is_active = 1
        db.commit()

        # 清除缓存
        pm.invalidate(agent_name)

        return {
            "status": "ok",
            "agent_name": agent_name,
            "activated_version": body.version,
        }
    except Exception as e:
        db.rollback()
        logger.error(f"激活 Prompt 版本失败: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        db.close()


@router.get("/api/admin/prompts/{agent_name}/active")
async def get_active(request: Request, agent_name: str):
    """获取当前活跃版本"""
    pm = _get_prompt_manager(request)
    if not pm:
        return JSONResponse({"error": "PromptManager 未初始化"}, status_code=503)

    # 先尝试从缓存获取版本信息
    info = pm.get_version_info(agent_name)
    if info.get("source") == "database":
        # 获取完整 prompt
        prompt_text = pm.get_prompt(agent_name, default_prompt="")
        info["prompt_text"] = prompt_text
        info["prompt_length"] = len(prompt_text)
    return info
