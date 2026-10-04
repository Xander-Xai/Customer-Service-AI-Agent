"""
FastAPI 应用工厂（v4.2 — 纯 ServiceContainer 模式）
用法: uvicorn api.app_factory:app --host 0.0.0.0 --port 8000

v4.2 改造：
- 完全使用 ServiceContainer 管理所有服务生命周期
- 消除 multi_agent_customer_service.py 模块级全局变量的导入
- App 同步创建（图在 lifespan 中异步构建）
"""

from contextlib import asynccontextmanager

from core.config import (
    API_KEY,
    API_KEY_ENABLED,
    CORS_ORIGINS,
    DEV_MODE,
    MONITORING_ADMIN_TOKEN,
    SESSION_TOKEN_SECRET,
)
from core.logger import get_logger, log_startup_failure

logger = get_logger("app_factory")

# ===== 数据库 + 管理员初始化（同步，模块加载时执行）=====
from db.database import init_db  # noqa: E402

init_db()

from auth.service import init_default_admin  # noqa: E402

init_default_admin()

# ===== 生产环境密钥校验已移至 core/config.py validate_required_config() =====
# app_factory.py 仅记录非致命的开发环境安全警告
if DEV_MODE:
    import core.config as _cfg
    if not _cfg.JWT_SECRET or any(p in _cfg.JWT_SECRET.lower() for p in ("change-me", "change_me", "your-", "dev-")):
        logger.warning("⚠️ JWT_SECRET 未配置或使用默认值，生产环境必须设置")
    if not _cfg.SESSION_TOKEN_SECRET or any(p in _cfg.SESSION_TOKEN_SECRET.lower() for p in ("change-me", "change_me", "your-", "dev-")):
        logger.warning("⚠️ SESSION_TOKEN_SECRET 未配置，生产环境必须设置")


# ===== 创建 ServiceContainer（同步创建基础设施组件）=====
from core.container import ServiceContainer  # noqa: E402

_container = ServiceContainer()


# ===== 应用生命周期（异步初始化容器中的 LLM/Agents/Router 等）=====
# 启动失败时随 CRITICAL 日志一起落盘的处置建议。只写可核对的具体项
# （配置项名 / 校验命令），不写"请检查配置"这类无法据以行动的措辞。
_STARTUP_REMEDIATION = (
    "按下方 traceback 定位根因后修复并重启进程 —— 本阶段 fail-closed，"
    "不会降级启动，也不会以半初始化状态对外服务。生产必查项："
    "LANGGRAPH_CHECKPOINT_BACKEND / LANGGRAPH_CHECKPOINT_DATABASE_URL 的 PostgreSQL 连通性、"
    "SESSION_STORAGE_BACKEND=redis、AGENT_RUN_DISPATCH=celery、"
    "JWT_SECRET / SESSION_TOKEN_SECRET / API_KEY 的强度与长度；"
    "可用 `make env-check` 与 `make runtime-verify` 复核。"
)


@asynccontextmanager
async def lifespan(app):
    """应用生命周期：异步初始化 ServiceContainer + 构建 LangGraph 图

    启动失败契约（fail closed）：任何异常都先落一条带完整 traceback、根因
    类型与处置建议的 CRITICAL 日志，再原样向上抛给 ASGI 服务器，绝不吞异常。
    进程即将退出时这条日志是唯一的取证入口，因此不能省，也不能只留
    uvicorn 自己那句无定位信息的 "Exception in 'lifespan' protocol"。
    """
    try:
        # 异步初始化（LLM、Agents、Router、RAG、Tools）
        await _container.initialize()
    except BaseException as exc:
        log_startup_failure(
            "container.initialize",
            exc,
            _STARTUP_REMEDIATION,
            logger=logger,
        )
        raise

    try:
        # v5.1: 将容器中的服务注入到 app.state（统一访问路径，消除模块级全局变量依赖）
        app.state.graph_app = _container.graph_app
        app.state.session_manager = _container.session_mgr
        app.state.response_cache = _container.cache
        app.state.metrics = _container.metrics
        app.state.message_bus = _container.bus
        app.state.sla_alert_mgr = _container.sla_alert_mgr
        app.state.circuit_breaker = _container.circuit_breaker

        # 同步更新模块级引用（向后兼容：_run_graph() 仍通过闭包引用这些变量）
        import api.app as _app_module

        _app_module._graph_app = _container.graph_app
        _app_module._session_manager = _container.session_mgr
        _app_module._response_cache = _container.cache
        _app_module._metrics = _container.metrics
        _app_module._bus = _container.bus
        _app_module._sla_alert_mgr = _container.sla_alert_mgr
        _app_module._circuit_breaker_ref = _container.circuit_breaker
    except BaseException as exc:
        log_startup_failure(
            "app_state_injection",
            exc,
            _STARTUP_REMEDIATION,
            logger=logger,
        )
        raise

    logger.info("✅ ServiceContainer 初始化完成，所有服务就绪")

    # v5.4: 缓存预热 — 后台异步执行，不阻塞启动
    try:
        from scripts.warm_cache import warm_cache_via_api

        async def _warm_background():
            import asyncio

            await asyncio.sleep(8)  # 等 LLM API 连接就绪
            try:
                await warm_cache_via_api("http://localhost:8000", max_concurrent=2)
            except Exception as e:
                logger.debug(f"缓存预热失败: {e}")

        import asyncio

        asyncio.create_task(_warm_background())
        logger.info("缓存预热任务已调度")
    except Exception as e:
        logger.debug(f"缓存预热跳过: {e}")

    yield

    # P1-3: 优雅关闭
    await _container.close()
    logger.info("ServiceContainer 已关闭")


# ===== 构建 FastAPI 应用（同步创建，图在 lifespan 中填充）=====
from api.app import create_app  # noqa: E402

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
from api.middleware import get_redis_client as _get_redis  # noqa: E402

app.state.get_redis_client = _get_redis

# 注册路由
from alerts.router import router as alerts_router  # noqa: E402
from api.routes.prompts import router as prompts_router  # noqa: E402
from auth.router import router as auth_router  # noqa: E402
from knowledge.router import router as knowledge_router  # noqa: E402

app.include_router(auth_router)
app.include_router(knowledge_router)
app.include_router(alerts_router)
app.include_router(prompts_router)

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
