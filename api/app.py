"""
FastAPI 应用工厂 + 核心路由（v5.0 — 路由拆分后）
职责：create_app() 工厂函数、静态资源、HTML 页面、图执行引擎。
中间件 → api/middleware.py | 聊天路由 → api/routes/chat.py
WebSocket → api/routes/ws.py | 监控 → api/routes/monitoring.py
"""

import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from core.config import (
    DEV_MODE,
    HTTPX_KEEPALIVE_CONNECTIONS,
    HTTPX_MAX_CONNECTIONS,
    RESPONSE_TIME_TARGET_MAX,
    RESPONSE_TIME_TARGET_MIN,
    VERSION,
)
from core.logger import get_logger, get_trace_id

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

# ── 缓存静态资源包装器（保证 Cache-Control 头，不依赖中间件）──
# FastAPI middleware 对 mounted sub-app 的响应头修改可能存在版本差异，
# 此包装器在 ASGI 层直接添加头，确保每个静态文件都能得到长缓存策略。


_UTF8_TEXT_CONTENT_TYPES = {
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "application/xml",
    "image/svg+xml",
}


def _header_name_bytes(name: str | bytes) -> bytes:
    return name.lower().encode("latin-1") if isinstance(name, str) else name.lower()


def _upsert_header(
    headers: list[tuple[bytes, bytes]], name: str | bytes, value: str | bytes
) -> list[tuple[bytes, bytes]]:
    name_bytes = _header_name_bytes(name)
    value_bytes = value.encode("latin-1") if isinstance(value, str) else value
    filtered = [(key, header_value) for key, header_value in headers if key.lower() != name_bytes]
    filtered.append((name_bytes, value_bytes))
    return filtered


def _get_header(headers: list[tuple[bytes, bytes]], name: str) -> str:
    name_bytes = _header_name_bytes(name)
    for key, value in headers:
        if key.lower() == name_bytes:
            return value.decode("latin-1")
    return ""


def _should_append_utf8_charset(content_type: str) -> bool:
    if not content_type:
        return False

    normalized = content_type.lower()
    if "charset=" in normalized:
        return False

    media_type = normalized.split(";", 1)[0].strip()
    return media_type.startswith("text/") or media_type in _UTF8_TEXT_CONTENT_TYPES


def _make_cached_static(
    app: ASGIApp, max_age: int = 31536000, extra_headers: dict | None = None
) -> ASGIApp:
    """包装 StaticFiles 子应用，为其所有响应添加缓存头。"""
    cc = f"public, max-age={max_age}, immutable"
    extras = list((extra_headers or {}).items())

    async def cached_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        original_send = send

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers = _upsert_header(headers, "cache-control", cc)
                headers = _upsert_header(headers, "x-content-type-options", "nosniff")

                content_type = _get_header(headers, "content-type")
                if _should_append_utf8_charset(content_type):
                    media_type = content_type.split(";", 1)[0].strip()
                    headers = _upsert_header(
                        headers, "content-type", f"{media_type}; charset=utf-8"
                    )

                for key, value in extras:
                    headers = _upsert_header(headers, key, value)
                message["headers"] = headers
            await original_send(message)

        await app(scope, receive, send_with_headers)

    return cached_app


# ── 图执行引擎（所有路由共用）──


async def _pending_steps(graph_app, graph_config: dict[str, Any]) -> tuple[str, ...]:
    """返回该 thread 最新 checkpoint 的待执行节点；无待执行返回空 tuple。

    探活失败一律按「无待执行」处理（正常执行），绝不让 checkpoint 查询异常阻断业务。
    """
    aget_state = getattr(graph_app, "aget_state", None)
    if aget_state is None:
        return ()
    try:
        snapshot = await aget_state(graph_config)
    except Exception:
        return ()
    if snapshot is None:
        return ()
    return tuple(getattr(snapshot, "next", ()) or ())


async def _run_graph(
    session_id: str,
    query: str,
    stream_callback=None,
    multimodal_content=None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """执行 LangGraph 图（原生异步 + SLA 告警 + 解决状态追踪）"""
    from core.shared_blackboard import set_blackboard_session_id

    set_blackboard_session_id(session_id)

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
    if multimodal_content:
        state["multimodal_content"] = multimodal_content
        state["has_multimodal"] = True
    if user_id:
        state["user_id"] = user_id

    # v5.2: 传递 thread_id config 以支持 checkpointer 断点续传
    # 无 checkpointer 时 config 被忽略，保持向后兼容
    # v6.4: per-thread 分布式锁 —— REST/SSE/WS/multimodal 共用同一执行边界，
    # 保证同一 thread 同一时刻只有一个 LangGraph Run 修改状态；不同 thread 并行。
    # 拿不到锁抛 ThreadBusyError（由全局 handler 映射 409 / SSE/WS 错误帧）。
    from core.concurrency.distributed_lock import thread_lock
    from core.streaming_context import reset_stream_callback, set_stream_callback

    graph_config = {"configurable": {"thread_id": session_id}}
    async with thread_lock(session_id):
        # 断点续跑：若该 thread 有**未完成**的 checkpoint（客户端断线重连、进程在
        # 图中途被杀），必须用 ainvoke(None) 续跑。LangGraph 语义实测：
        #   ainvoke(None, cfg)  -> 从 checkpoint 的 next 继续，不重跑已完成节点
        #   ainvoke(state, cfg) -> 从 START 重新执行并用入参覆盖 channel 值
        # 因此传 state 会把"续传"退化成"从头重跑"。已完成的历史快照（next 为空）
        # 走正常分支，多轮对话语义不变。
        pending = await _pending_steps(_graph_app, graph_config)
        if pending:
            logger.info("从 checkpoint 续跑 session=%s pending=%s", session_id, pending)
            graph_input = None
        else:
            graph_input = state

        # 流式回调**不能**放进 state：state 的每个 channel 都会被 checkpointer 序列化，
        # async 可调用对象会触发 "Type is not msgpack serializable: function"，
        # MemorySaver 与官方 AsyncPostgresSaver 都会写盘失败。改用 contextvar 传递，
        # 节点通过 core.streaming_context.get_stream_callback(state) 读取。
        stream_token = set_stream_callback(stream_callback)
        try:
            try:
                result = await _graph_app.ainvoke(graph_input, config=graph_config)
            except (AttributeError, TypeError):
                # 无 checkpointer 的旧图：不接受 config。续跑模式没有 state 可传，
                # 此时退回有 config 的调用会必然失败，直接抛原始错误更有诊断价值。
                if graph_input is None:
                    raise
                try:
                    result = await _graph_app.ainvoke(graph_input)
                except AttributeError:
                    result = await asyncio.to_thread(_graph_app.invoke, graph_input)
        finally:
            reset_stream_callback(stream_token)

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
            trust_env=False,
        )
        logger.info(
            f"httpx 连接池就绪 (max={HTTPX_MAX_CONNECTIONS} / keepalive={HTTPX_KEEPALIVE_CONNECTIONS})"
        )

        # 周期性 WebSocket 连接清理
        from api.routes.ws import periodic_ws_cleanup

        cleanup_task = asyncio.create_task(periodic_ws_cleanup())

        # 周期性限流清理 (由 setup_middleware 注入)
        rate_limit_cleanup_task = None
        if hasattr(app.state, "start_rate_limit_cleanup"):
            rate_limit_cleanup_task = app.state.start_rate_limit_cleanup()

        # v5.0: circuit_breaker 到 app.state
        container = getattr(app.state, "container", None)
        if container and container.circuit_breaker:
            app.state.circuit_breaker = container.circuit_breaker
        elif _circuit_breaker_ref:
            app.state.circuit_breaker = _circuit_breaker_ref

        yield

        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

        if rate_limit_cleanup_task:
            rate_limit_cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rate_limit_cleanup_task

        await app.state.http_client.aclose()
        from llm.client import OpenAICompatibleClient

        await OpenAICompatibleClient.close_all_clients()

        # v6.4: 释放 API 执行边界的 per-thread 锁管理器
        from core.concurrency.distributed_lock import shutdown_api_lock_manager

        await shutdown_api_lock_manager()
        logger.info("httpx 连接池已关闭")

    app = FastAPI(title="药妆智多星多智能体客服系统", version=VERSION, lifespan=lifespan)

    # ── 存储依赖到 app.state（供路由模块访问）──
    app.state.session_manager = session_manager
    app.state.response_cache = response_cache
    app.state.metrics = metrics
    app.state.message_bus = message_bus
    app.state.sla_alert_mgr = sla_alert_mgr
    app.state.graph_app = graph_app  # v5.1: 统一通过 app.state 访问
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
        allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"],
        allow_headers=[
            "X-API-Key",
            "X-Admin-Token",
            "X-Session-Token",
            "X-CSRF-Token",
            "Idempotency-Key",
            "Content-Type",
            "Authorization",
        ],
    )
    setup_middleware(app)

    # ── v6.4: THREAD_BUSY -> 409（同一 thread 并发请求的统一错误契约）──
    from core.concurrency.distributed_lock import (
        ThreadBusyError,
        ThreadLockUnavailableError,
        thread_busy_payload,
    )

    async def _thread_busy_handler(request: Request, exc: Exception):
        from fastapi.responses import JSONResponse

        assert isinstance(exc, ThreadBusyError)
        logger.info("THREAD_BUSY thread=%s request=%s", exc.thread_id, request.url.path)
        return JSONResponse(thread_busy_payload(exc), status_code=409)

    async def _thread_lock_unavailable_handler(request: Request, exc: Exception):
        from fastapi.responses import JSONResponse

        logger.error("THREAD_LOCK_UNAVAILABLE path=%s", request.url.path)
        return JSONResponse(
            {"error": "THREAD_LOCK_UNAVAILABLE", "code": "THREAD_LOCK_UNAVAILABLE"},
            status_code=503,
        )

    app.add_exception_handler(ThreadBusyError, _thread_busy_handler)
    app.add_exception_handler(ThreadLockUnavailableError, _thread_lock_unavailable_handler)

    # ── 挂载路由模块 ──
    from api.routes.approvals import router as approvals_router
    from api.routes.chat import router as chat_router
    from api.routes.chat_multimodal import router as chat_multimodal_router
    from api.routes.feedback import router as feedback_router
    from api.routes.monitoring import router as monitoring_router
    from api.routes.runs import router as runs_router
    from api.routes.sessions import router as sessions_router
    from api.routes.ws import router as ws_router

    app.include_router(monitoring_router)
    app.include_router(sessions_router)
    app.include_router(feedback_router)
    app.include_router(chat_router)
    app.include_router(chat_multimodal_router)
    app.include_router(runs_router)
    app.include_router(approvals_router)
    app.include_router(ws_router)

    # ── 静态资源（使用缓存包装器，不依赖中间件）──
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    web_dir = os.path.join(project_root, "web")
    static_dir = os.path.join(web_dir, "static")
    dist_dir = os.path.join(web_dir, "static", "dist")

    if os.path.isdir(static_dir):
        cached_static = _make_cached_static(StaticFiles(directory=static_dir))
        app.mount("/static", cached_static, name="static")
        logger.info(f"静态资源已挂载: {static_dir}")

    # /styles/ → 样式文件，长缓存 + immutable
    styles_dir = os.path.join(web_dir, "styles")
    if os.path.isdir(styles_dir):
        cached_styles = _make_cached_static(StaticFiles(directory=styles_dir))
        app.mount("/styles", cached_styles, name="styles")
        logger.info(f"样式文件已挂载: {styles_dir}")

    # /assets/ → Vite 构建产物（含 hash 文件名），长缓存 1 年 + immutable
    assets_dir = os.path.join(dist_dir, "assets")
    if os.path.isdir(assets_dir):
        cached_assets = _make_cached_static(
            StaticFiles(directory=assets_dir),
            extra_headers={"X-Content-Type-Options": "nosniff"},
        )
        app.mount("/assets", cached_assets, name="assets")
        logger.info(f"构建资源已挂载: {assets_dir}")

    def _serve_html(request: Request, file_path: str, fallback_msg: str):
        if os.path.exists(file_path):
            nonce = getattr(request.state, "csp_nonce", "")
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
            if nonce:
                content = content.replace("<script", f'<script nonce="{nonce}"')
            return HTMLResponse(
                content=content, headers={"Content-Type": "text/html; charset=utf-8"}
            )
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
        widget_path = os.path.join(web_dir, "widget.html")
        return _serve_html(request, widget_path, "Widget 未找到")

    @app.get("/theme-comparison.html", response_class=HTMLResponse)
    async def serve_theme_comparison(request: Request):
        tc_path = os.path.join(web_dir, "theme-comparison.html")
        return _serve_html(request, tc_path, "主题对比页面未找到")

    return app
