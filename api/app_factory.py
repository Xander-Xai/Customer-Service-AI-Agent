"""
FastAPI 应用工厂（v4.2 — 纯 ServiceContainer 模式）
用法: uvicorn api.app_factory:app --host 0.0.0.0 --port 8000

v4.2 改造：
- 完全使用 ServiceContainer 管理所有服务生命周期
- 消除 multi_agent_customer_service.py 模块级全局变量的导入
- App 同步创建（图在 lifespan 中异步构建）
"""

import asyncio
from contextlib import asynccontextmanager

from config import (
    API_KEY,
    API_KEY_ENABLED,
    CORS_ORIGINS,
    DEV_MODE,
    MONITORING_ADMIN_TOKEN,
    SESSION_TOKEN_SECRET,
)
from logger import get_logger

logger = get_logger("app_factory")

# ===== 数据库 + 管理员初始化（同步，模块加载时执行）=====
from db.database import init_db

init_db()

from auth.service import init_default_admin

init_default_admin()

# ===== 生产环境强制校验安全密钥 =====
import config as _cfg

_security_errors = []

if not _cfg.JWT_SECRET or _cfg.JWT_SECRET in ("", "change-me-in-production"):
    if not DEV_MODE:
        _security_errors.append("JWT_SECRET 未配置或使用默认值，生产环境必须设置")

if not DEV_MODE:
    if not _cfg.SESSION_TOKEN_SECRET or _cfg.SESSION_TOKEN_SECRET in (
        "",
        "change-me-session-secret-in-production",
    ):
        _security_errors.append("SESSION_TOKEN_SECRET 未配置，会话校验将被禁用")

if _security_errors:
    for err in _security_errors:
        logger.error(f"🚨 安全启动检查失败: {err}")
    if not DEV_MODE:
        raise SystemExit("安全配置不满足生产要求，请检查 .env 文件")


# ===== 创建 ServiceContainer（同步创建基础设施组件）=====
from core.container import ServiceContainer

_container = ServiceContainer()


# ===== 应用生命周期（异步初始化容器中的 LLM/Agents/Router 等）=====
@asynccontextmanager
async def lifespan(app):
    """应用生命周期：异步初始化 ServiceContainer + 构建 LangGraph 图"""
    # 异步初始化（LLM、Agents、Router、RAG、Tools）
    await _container.initialize()

    # 将容器中的服务注入到 api/app.py 的全局引用（供中间件和端点使用）
    import api.app as _app_module

    _app_module._session_manager = _container.session_mgr
    _app_module._response_cache = _container.cache
    _app_module._metrics = _container.metrics
    _app_module._bus = _container.bus
    _app_module._sla_alert_mgr = _container.sla_alert_mgr
    _app_module._graph_app = _container.graph_app
    _app_module._circuit_breaker_ref = _container.circuit_breaker  # P0-1: 注入熔断器引用

    logger.info("✅ ServiceContainer 初始化完成，所有服务就绪")

    yield

    # P1-3: 优雅关闭
    await _container.close()
    logger.info("ServiceContainer 已关闭")


# ===== 构建 FastAPI 应用（同步创建，图在 lifespan 中填充）=====
from api.app import create_app

# 初始图为空，lifespan 中会通过容器构建并注入
app = create_app(
    None,  # graph_app 在 lifespan 中设置
    session_manager=_container.session_mgr,
    response_cache=_container.cache,
    metrics=_container.metrics,
    message_bus=_container.bus,
    sla_alert_mgr=_container.sla_alert_mgr,
)

# 注入容器到 app.state
app.state.container = _container

# 注入 Redis 客户端工厂到 app.state（供健康检查等使用）
from api.middleware import get_redis_client as _get_redis

app.state.get_redis_client = _get_redis

# 注册路由
from alerts.router import router as alerts_router
from auth.router import router as auth_router
from knowledge.router import router as knowledge_router

app.include_router(auth_router)
app.include_router(knowledge_router)
app.include_router(alerts_router)

# 设置 lifespan
app.router.lifespan_context = lifespan

# P2-1: OpenTelemetry 分布式追踪（可选）
try:
    from core.tracing import setup_tracing

    setup_tracing(app)
except Exception as e:
    logger.debug(f"分布式追踪初始化跳过: {e}")

# ===== 启动安全检查 =====
_security_warnings = []

if not API_KEY_ENABLED:
    _security_warnings.append(
        "API_KEY_ENABLED=false，所有端点未认证！生产环境必须启用 API Key 认证。"
    )

if API_KEY_ENABLED and (not API_KEY or API_KEY in ("", "change-me-in-production")):
    _security_warnings.append("API_KEY 未设置或使用默认值，认证将拒绝所有请求或不安全。")

if not MONITORING_ADMIN_TOKEN or MONITORING_ADMIN_TOKEN in ("", "change-me-monitoring-token"):
    _security_warnings.append("MONITORING_ADMIN_TOKEN 未设置或使用默认值，监控端点安全受限。")

if not SESSION_TOKEN_SECRET or SESSION_TOKEN_SECRET in (
    "",
    "change-me-session-secret-in-production",
):
    _security_warnings.append("SESSION_TOKEN_SECRET 未设置或使用默认值，会话所有权校验将被禁用。")

if CORS_ORIGINS and "*" in CORS_ORIGINS:
    _security_warnings.append("CORS_ORIGINS 包含 *，生产环境请配置具体域名。")

for w in _security_warnings:
    logger.warning(f"⚠️ 安全警告: {w}")

if not _security_warnings:
    logger.info("✅ 安全检查通过，所有安全配置就绪。")

logger.info("✅ v4.2 启动完成：ServiceContainer 纯容器模式 + 安全加固已就绪")
