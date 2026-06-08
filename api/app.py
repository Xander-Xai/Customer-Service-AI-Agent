"""
FastAPI 应用工厂 + 核心路由（v5.0 — 路由拆分后）
职责：create_app() 工厂函数、静态资源、HTML 页面、图执行引擎。
中间件 → api/middleware.py | 聊天路由 → api/routes/chat.py
WebSocket → api/routes/ws.py | 监控 → api/routes/monitoring.py
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import (
    DEV_MODE,
    HTTPX_KEEPALIVE_CONNECTIONS,
    HTTPX_MAX_CONNECTIONS,
    RESPONSE_TIME_TARGET_MAX,
    RESPONSE_TIME_TARGET_MIN,
    VERSION,
)
from logger import get_logger, get_trace_id

logger = get_logger("api")

# ── 模块加载时间（用于 uptime 计算）──
_MODULE_LOAD_TIME = time.time()

# ── 全局引用（由 app_factory.py 在 lifespan 中注入）──
_graph_app = None
_session_manager = None
_response_cache = None
_metrics = None
_bus = None
_sla_alert_mgr = None
_redis_client = None
_circuit_breaker_ref = None


# ── 图执行引擎（所有路由共用）──


async def _run_graph(
    session_id: str, query: str, stream_callback=None, multimodal_content=None
) -> dict[str, Any]:
    """执行 LangGraph 图（原生异步 + SLA 告警 + 解决状态追踪）"""
    start = time.time()

    state = {
        "session_id": session_id,
        "current_agent": "",
        "customer_query": query,
        "query_type": "",
        "response": "",
        "complexity": 0,
        "fast_path": True,
        "collaboration_mode": "",
        "cached": False,
        "agents_used": [],
        "resolution_status": "",
        "trace_id": get_trace_id(),
    }
    if stream_callback:
        state["stream_callback"] = stream_callback
    if multimodal_content:
        state["multimodal_content"] = multimodal_content
        state["has_multimodal"] = True

    try:
        result = await _graph_app.ainvoke(state)
    except AttributeError:
        result = await asyncio.to_thread(_graph_app.invoke, state)

    elapsed = time.time() - start
    result["elapsed"] = elapsed

    prefix = "[SLA-Stream]" if stream_callback else "[SLA]"
    if elapsed > RESPONSE_TIME_TARGET_MAX:
        logger.warning(
            f"{prefix} 响应超时: {elapsed:.2f}s > {RESPONSE_TIME_TARGET_MAX}s "
            f"(session={session_id}, mode={result.get('collaboration_mode', '')}, "
            f"agent={result.get('current_agent', '')})"
        )
    elif elapsed < RESPONSE_TIME_TARGET_MIN:
        logger.info(
            f"{prefix} 响应偏快: {elapsed:.2f}s < {RESPONSE_TIME_TARGET_MIN}s "
            f"(session={session_id}, cached={result.get('cached', False)})"
        )

    if _metrics:
        await _metrics.record_request(
            elapsed=elapsed,
            agent=result.get("current_agent", ""),
            mode=result.get("collaboration_mode", ""),
            cached=result.get("cached", False),
            session_id=session_id,
            escalated=result.get("collaboration_mode", "") == "hierarchical",
            resolution_status=result.get("resolution_status", ""),
        )
        if _sla_alert_mgr:
            try:
                await _sla_alert_mgr.check_and_alert(_metrics)
            except Exception as e:
                logger.debug(f"SLA 告警检查失败: {e}")

    if stream_callback:
        try:
            await stream_callback(None)
        except Exception as e:
            logger.debug(f"stream_callback 终止信号失败: {e}")

    return result


async def _persist_metrics_snapshot():
    """将指标快照持久化到 Redis（静默失败）"""
    if not _metrics:
        return
    from api.middleware import get_redis_client

    r = get_redis_client()
    if r:
        try:
            await _metrics.save_snapshot(r)
        except Exception as e:
            logger.debug(f"指标快照持久化失败: {e}")


# ── 应用工厂 ──


def create_app(
    graph_app,
    session_manager=None,
    response_cache=None,
    metrics=None,
    message_bus=None,
    sla_alert_mgr=None,
):
    """创建 FastAPI 应用"""
    global _graph_app, _session_manager, _response_cache, _metrics, _bus, _sla_alert_mgr
    _graph_app = graph_app
    _session_manager = session_manager
    _response_cache = response_cache
    _metrics = metrics
    _bus = message_bus
    _sla_alert_mgr = sla_alert_mgr

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(
                max_connections=HTTPX_MAX_CONNECTIONS,
                max_keepalive_connections=HTTPX_KEEPALIVE_CONNECTIONS,
            ),
        )
        logger.info(
            f"httpx 连接池就绪 (max={HTTPX_MAX_CONNECTIONS} / keepalive={HTTPX_KEEPALIVE_CONNECTIONS})"
        )

        # 周期性 WebSocket 连接清理
        from api.routes.ws import periodic_ws_cleanup

        cleanup_task = asyncio.create_task(periodic_ws_cleanup())

        # v5.0: circuit_breaker 到 app.state
        container = getattr(app.state, "container", None)
        if container and container.circuit_breaker:
            app.state.circuit_breaker = container.circuit_breaker
        elif _circuit_breaker_ref:
            app.state.circuit_breaker = _circuit_breaker_ref

        yield

        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

        await app.state.http_client.aclose()
        from llm.client import OpenAICompatibleClient

        await OpenAICompatibleClient.close_all_clients()
        logger.info("httpx 连接池已关闭")

    app = FastAPI(title="药妆智多星多智能体客服系统", version=VERSION, lifespan=lifespan)

    # ── 存储依赖到 app.state（供路由模块访问）──
    app.state.session_manager = session_manager
    app.state.response_cache = response_cache
    app.state.metrics = metrics
    app.state.message_bus = message_bus
    app.state.sla_alert_mgr = sla_alert_mgr
    app.state.circuit_breaker = None
    app.state.dev_mode = DEV_MODE
    app.state.module_load_time = _MODULE_LOAD_TIME
    app.state.run_graph = _run_graph
    app.state.persist_metrics_snapshot = _persist_metrics_snapshot

    # ── 中间件栈 ──
    from api.middleware import setup_middleware
    from api.utils import resolve_cors_origins

    resolved_origins = resolve_cors_origins()
    logger.info(f"CORS 允许来源: {resolved_origins}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_origins,
        allow_methods=["GET", "POST", "DELETE", "PUT"],
        allow_headers=[
            "X-API-Key",
            "X-Admin-Token",
            "X-Session-Token",
            "X-CSRF-Token",
            "Content-Type",
            "Authorization",
        ],
    )
    setup_middleware(app)

    # ── 挂载路由模块 ──
    from api.routes.chat import router as chat_router
    from api.routes.feedback import router as feedback_router
    from api.routes.monitoring import router as monitoring_router
    from api.routes.sessions import router as sessions_router
    from api.routes.ws import router as ws_router

    app.include_router(monitoring_router)
    app.include_router(sessions_router)
    app.include_router(feedback_router)
    app.include_router(chat_router)
    app.include_router(ws_router)

    # ── 静态资源 ──
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static_dir = os.path.join(project_root, "static")
    dist_dir = os.path.join(project_root, "static", "dist")

    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
        logger.info(f"静态资源已挂载: {static_dir}")

    def _serve_html(request: Request, file_path: str, fallback_msg: str):
        if os.path.exists(file_path):
            nonce = getattr(request.state, "csp_nonce", "")
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
            if nonce:
                content = content.replace("<script", f'<script nonce="{nonce}"')
            return HTMLResponse(content=content)
        return HTMLResponse(f"<h1>{fallback_msg}</h1>", status_code=404)

    def _html_path(filename: str) -> str:
        return os.path.join(dist_dir, filename)

    @app.get("/", response_class=HTMLResponse)
    async def serve_index(request: Request):
        return _serve_html(request, _html_path("index.html"), "前端文件未找到")

    @app.get("/login.html", response_class=HTMLResponse)
    async def serve_login(request: Request):
        return _serve_html(request, _html_path("login.html"), "登录页面未找到")

    @app.get("/admin.html", response_class=HTMLResponse)
    async def serve_admin(request: Request):
        return _serve_html(request, _html_path("admin.html"), "管理后台未找到")

    @app.get("/widget.html", response_class=HTMLResponse)
    async def serve_widget(request: Request):
        widget_path = os.path.join(project_root, "widget.html")
        return _serve_html(request, widget_path, "Widget 未找到")

    return app
