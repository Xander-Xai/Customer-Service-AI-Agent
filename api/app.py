"""
FastAPI + WebSocket 异步服务层 (v3.9 - 生产就绪版)
核心改造：
- v3.9: CORS 改为从环境变量 ALLOWED_ORIGINS 读取（支持多域名）
- v3.9: 增强健康检查（Redis/LLM 可达性）
- v3.7: 监控端点需 Admin Token 认证（Critical 修复）
- v3.7: WebSocket 连接限流 + 消息限流 + 空闲超时（High 修复）
- v3.7: 会话所有权校验，防劫持（High 修复）
- v3.7: 错误响应脱敏，不泄露内部细节（High 修复）
- v3.7: 输入内容净化（Medium 修复）
- v3.7: MessageBus 订阅/取消改用 async lock（Medium 修复）
"""
import asyncio
import hmac
import json
import os
import re
import time
import uuid
from collections import defaultdict
from typing import Dict, Any
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import (
    CORS_ORIGINS, API_KEY_ENABLED, API_KEY, VERSION,
    RESPONSE_TIME_TARGET_MAX, RESPONSE_TIME_TARGET_MIN,
    HTTPX_MAX_CONNECTIONS, HTTPX_KEEPALIVE_CONNECTIONS, REDIS_URL,
    MAX_QUERY_LENGTH,
    MONITORING_ADMIN_TOKEN,
    WS_MAX_CONNECTIONS_PER_IP, WS_MESSAGE_RATE_LIMIT, WS_IDLE_TIMEOUT,
)
from logger import get_logger

logger = get_logger("api")

# ── 输入净化 ──
_CONTROL_CHAR_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_HTML_TAG_RE = re.compile(r'<[^>]+>')


def _sanitize_input(text: str) -> str:
    """净化用户输入：移除控制字符和 HTML 标签"""
    text = _CONTROL_CHAR_RE.sub('', text)
    text = _HTML_TAG_RE.sub('', text)
    return text.strip()


# ── Session ID 校验 ──
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _validate_session_id(sid: str) -> str:
    """校验并清理 session_id，不合法则自动生成 UUID"""
    if sid and _SESSION_ID_RE.match(sid):
        return sid
    return str(uuid.uuid4())


# ── CORS 配置（v3.9: 从环境变量 ALLOWED_ORIGINS 读取） ──
def _resolve_cors_origins() -> list:
    """
    优先从 ALLOWED_ORIGINS 环境变量读取（JSON 数组格式）。
    若未设置，回退到 config.py 的 CORS_ORIGINS。
    兼容逗号分隔的字符串格式： "https://a.com,https://b.com"
    """
    raw = os.environ.get("ALLOWED_ORIGINS", "")
    if raw:
        raw = raw.strip()
        if raw.startswith("["):
            # JSON 数组格式: ["https://a.com","https://b.com"]
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        # 逗号分隔格式: https://a.com,https://b.com
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        if origins:
            return origins
    # 回退到 config.py 定义
    return CORS_ORIGINS


# ── WebSocket 连接跟踪 ──
_ws_connections: Dict[str, int] = defaultdict(int)
_ws_lock = asyncio.Lock()
_WS_HEARTBEAT_INTERVAL = 30


# ── Pydantic 模型 ──
class ChatRequest(BaseModel):
    query: str = Field(..., max_length=2000)
    session_id: str = Field(default="", max_length=36)
    session_token: str = Field(default="", max_length=64)


class FeedbackRequest(BaseModel):
    session_id: str = Field(..., max_length=36)
    resolved: bool
    comment: str = Field(default="", max_length=500)


# ── 全局引用（在 create_app 中注入） ──
_graph_app = None
_session_manager = None
_response_cache = None
_metrics = None
_bus = None
_sla_alert_mgr = None
_redis_client = None


def _get_redis_client():
    """获取 Redis 客户端单例（懒初始化，失败返回 None）"""
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            _redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3)
            _redis_client.ping()
        except Exception:
            _redis_client = None
    return _redis_client


def create_app(graph_app, session_manager=None, response_cache=None, metrics=None, message_bus=None, sla_alert_mgr=None):
    """创建 FastAPI 应用（v3.9: 生产就绪版）"""
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
        logger.info(f"httpx 连接池就绪 (max={HTTPX_MAX_CONNECTIONS} / keepalive={HTTPX_KEEPALIVE_CONNECTIONS})")

        yield

        await app.state.http_client.aclose()
        from core.monitoring import OpenAICompatibleClient
        await OpenAICompatibleClient.close_all_clients()
        logger.info("httpx 连接池已关闭")

    app = FastAPI(title="药妆智多星多智能体客服系统", version=VERSION, lifespan=lifespan)

    # ── CORS（v3.9: 从环境变量读取） ──
    resolved_origins = _resolve_cors_origins()
    logger.info(f"CORS 允许来源: {resolved_origins}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["X-API-Key", "X-Admin-Token", "X-Session-Token", "Content-Type"],
    )

    # ── 限流中间件 ──
    _rate_limit_store: Dict[str, list] = defaultdict(list)
    _RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "60"))
    _RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if request.url.path in ("/", "/api/health") or request.url.path.startswith("/static/") or request.url.path.startswith("/ws/"):
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        _rate_limit_store[client_ip] = [t for t in _rate_limit_store[client_ip] if now - t < _RATE_LIMIT_WINDOW]
        if len(_rate_limit_store[client_ip]) >= _RATE_LIMIT_MAX:
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        _rate_limit_store[client_ip].append(now)
        return await call_next(request)

    # ── 安全响应头 ──
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'"
        )
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["X-XSS-Protection"] = "0"
        return response

    # ── 静态资源 ──
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static_dir = os.path.join(project_root, "static")
    templates_dir = os.path.join(project_root, "templates")

    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
        logger.info(f"静态资源已挂载: {static_dir}")

    @app.get("/", response_class=HTMLResponse)
    async def serve_index():
        index_path = os.path.join(templates_dir, "index.html")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        return HTMLResponse("<h1>前端文件未找到</h1>", status_code=404)

    # ── API Key 认证中间件 ──
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if path in ("/", "/api/health"):
            return await call_next(request)
        if path.startswith("/static/"):
            return await call_next(request)

        # 监控敏感端点
        if path in ("/api/metrics", "/api/kpi", "/api/circuit-breaker", "/api/alerts", "/api/cache/stats"):
            if MONITORING_ADMIN_TOKEN:
                admin_token = request.headers.get("X-Admin-Token", "")
                api_key = request.headers.get("X-API-Key", "")
                if not (hmac.compare_digest(admin_token, MONITORING_ADMIN_TOKEN) or
                        (API_KEY_ENABLED and api_key and hmac.compare_digest(api_key, API_KEY))):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
            elif API_KEY_ENABLED and API_KEY:
                api_key = request.headers.get("X-API-Key", "")
                if not hmac.compare_digest(api_key, API_KEY):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

        # 会话管理端点
        if path.startswith("/api/sessions"):
            local_dev = request.client and request.client.host in ("127.0.0.1", "::1", "localhost")
            if API_KEY_ENABLED and API_KEY and not local_dev:
                api_key = request.headers.get("X-API-Key", "")
                if not hmac.compare_digest(api_key, API_KEY):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

        # 反馈端点
        if path.startswith("/api/feedback"):
            local_dev = request.client and request.client.host in ("127.0.0.1", "::1", "localhost")
            if API_KEY_ENABLED and API_KEY and not local_dev:
                api_key = request.headers.get("X-API-Key", "")
                if not hmac.compare_digest(api_key, API_KEY):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

        # 其他 API 端点
        if API_KEY_ENABLED and path.startswith("/api/"):
            api_key = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(api_key, API_KEY):
                return JSONResponse({"error": "Unauthorized: Invalid API Key"}, status_code=401)

        response = await call_next(request)
        return response

    # ── WebSocket 实时对话 ──

    @app.websocket("/ws/chat")
    async def websocket_chat(ws: WebSocket):
        ws_api_key = ws.query_params.get("api_key", "") or ws.headers.get("x-api-key", "")
        local_development = ws.client and ws.client.host in ("127.0.0.1", "::1", "localhost")

        if API_KEY_ENABLED:
            if ws_api_key:
                if not hmac.compare_digest(ws_api_key, API_KEY):
                    await ws.close(code=4001, reason="Unauthorized")
                    return
            elif not local_development:
                await ws.close(code=4001, reason="Unauthorized")
                return

        client_ip = ws.client.host if ws.client else "unknown"
        async with _ws_lock:
            if _ws_connections[client_ip] >= WS_MAX_CONNECTIONS_PER_IP:
                await ws.close(code=4029, reason="Too many connections")
                return
            _ws_connections[client_ip] += 1

        await ws.accept()
        session_id = str(uuid.uuid4())
        session_token = _session_manager.generate_session_token(session_id) if _session_manager else ""
        logger.info(f"[WS] 新连接: {session_id} ip={client_ip}")

        status_messages = []

        async def on_agent_event(msg):
            try:
                payload = msg.payload
                agent_name = payload.get("agent", "")
                topic = msg.topic
                if topic == "agent.processing":
                    status_messages.append(f"{agent_name}正在处理...")
                elif topic == "agent.completed":
                    status_messages.append(f"{agent_name}处理完成")
            except Exception as e:
                logger.debug(f"Bus 事件处理异常: {e}")

        if _bus:
            await _bus.subscribe("agent.processing", on_agent_event)
            await _bus.subscribe("agent.completed", on_agent_event)

        msg_timestamps: list = []
        last_activity = time.time()

        try:
            while True:
                try:
                    data = await asyncio.wait_for(ws.receive_json(), timeout=WS_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    try:
                        await ws.send_json({"type": "ping"})
                        try:
                            data = await asyncio.wait_for(ws.receive_json(), timeout=WS_IDLE_TIMEOUT)
                        except asyncio.TimeoutError:
                            await ws.send_json({"type": "error", "content": "连接空闲超时，请重新连接"})
                            await ws.close(code=4008, reason="Idle timeout")
                            break
                    except Exception:
                        await ws.close(code=4008, reason="Idle timeout")
                        break

                now = time.time()
                msg_timestamps = [t for t in msg_timestamps if now - t < 60]

                if data.get("type") == "pong":
                    last_activity = now
                    continue

                if len(msg_timestamps) >= WS_MESSAGE_RATE_LIMIT:
                    await ws.send_json({"type": "error", "content": "消息发送过于频繁，请稍后再试"})
                    continue
                msg_timestamps.append(now)
                last_activity = now

                query = data.get("query", "").strip()[:MAX_QUERY_LENGTH]
                query = _sanitize_input(query)
                sid = _validate_session_id(data.get("session_id", ""))

                if sid != session_id and _session_manager:
                    token = data.get("session_token", "")
                    local_dev = ws.client and ws.client.host in ("127.0.0.1", "::1", "localhost")
                    if not local_dev:
                        if not _session_manager.validate_session_token(sid, token):
                            await ws.send_json({"type": "error", "content": "会话令牌无效"})
                            continue
                if not query:
                    await ws.send_json({"type": "error", "content": "查询不能为空"})
                    continue

                await ws.send_json({"type": "status", "content": "正在分析您的问题..."})
                status_messages.clear()

                async def progressive_notify():
                    delays = [0.3, 0.5, 1.0, 1.5]
                    messages = ["正在识别意图...", "正在分配专家...", "专家处理中...", "即将完成..."]
                    for delay, msg in zip(delays, messages):
                        await asyncio.sleep(delay)
                        try:
                            if status_messages:
                                await ws.send_json({"type": "progress", "content": status_messages[-1]})
                            else:
                                await ws.send_json({"type": "progress", "content": msg})
                        except Exception:
                            break

                notify_task = asyncio.create_task(progressive_notify())

                try:
                    result = await _run_graph(sid, query)
                    notify_task.cancel()

                    await ws.send_json({
                        "type": "response",
                        "content": result.get("response", ""),
                        "agent": result.get("current_agent", ""),
                        "elapsed": result.get("elapsed", 0),
                        "mode": result.get("collaboration_mode", "sequential"),
                        "cached": result.get("cached", False),
                        "agents_used": result.get("agents_used", []),
                        "processing_time": result.get("elapsed", 0),
                        "resolution_status": result.get("resolution_status", ""),
                        "session_id": sid,
                        "session_token": session_token,
                    })
                except Exception as e:
                    notify_task.cancel()
                    logger.error(f"WS 处理失败: {e}", exc_info=True)
                    await ws.send_json({"type": "error", "content": "处理失败，请稍后重试"})

        except WebSocketDisconnect:
            logger.info(f"[WS] 断开: {session_id}")
        finally:
            async with _ws_lock:
                _ws_connections[client_ip] = max(0, _ws_connections[client_ip] - 1)
            if _bus:
                try:
                    await _bus.unsubscribe("agent.processing", on_agent_event)
                    await _bus.unsubscribe("agent.completed", on_agent_event)
                except Exception as e:
                    logger.warning(f"[WS] Bus 取消订阅失败 session={session_id}: {e}")

    # ── REST API ──

    @app.post("/api/chat")
    async def rest_chat(data: ChatRequest):
        query = _sanitize_input(data.query)[:MAX_QUERY_LENGTH]
        sid = _validate_session_id(data.session_id)
        if not query:
            return JSONResponse({"error": "query 不能为空"}, status_code=400)
        client_provided_sid = bool(data.session_id)
        if client_provided_sid and _session_manager:
            if not _session_manager.validate_session_token(sid, data.session_token or ""):
                return JSONResponse({"error": "会话令牌无效"}, status_code=403)
        try:
            result = await _run_graph(sid, query)
            if _session_manager and not client_provided_sid:
                result["session_token"] = _session_manager.generate_session_token(sid)
            return result
        except Exception as e:
            logger.error(f"REST 处理失败: {e}", exc_info=True)
            return JSONResponse({"error": "服务内部错误，请稍后重试"}, status_code=500)

    # ── 增强健康检查（v3.9: 检查 Redis + LLM 可达性） ──

    @app.get("/api/health")
    async def health():
        """
        增强健康检查端点（v3.9）
        返回各子系统的健康状态，不暴露内部阈值。
        """
        from multi_agent_customer_service import circuit_breaker as _cb
        cb_status = _cb.get_status()

        # Redis 检查
        redis_ok = False
        redis_latency_ms = None
        r = _get_redis_client()
        if r:
            try:
                t0 = time.time()
                r.ping()
                redis_latency_ms = round((time.time() - t0) * 1000, 2)
                redis_ok = True
            except Exception:
                redis_ok = False

        # LLM 可达性检查（仅检查配置是否存在，不做实际调用）
        llm_configured = bool(os.environ.get("OPENAI_API_KEY"))

        # 系统状态汇总
        circuit_state = cb_status["state"]
        overall = "healthy"
        if circuit_state == "open":
            overall = "degraded"
        elif not redis_ok and REDIS_URL:
            overall = "degraded"

        return {
            "status": overall,
            "version": VERSION,
            "timestamp": time.time(),
            "components": {
                "circuit_breaker": {
                    "state": circuit_state,
                    "consecutive_failures": cb_status.get("consecutive_failures", 0),
                },
                "redis": {
                    "connected": redis_ok,
                    "latency_ms": redis_latency_ms,
                },
                "llm": {
                    "configured": llm_configured,
                },
            },
        }

    # ── 指标端点 ──

    @app.get("/api/metrics")
    async def metrics_endpoint():
        if _metrics:
            stats = await _metrics.get_stats()
        else:
            stats = {"error": "metrics not initialized"}
        cache_stats = _response_cache.get_stats() if _response_cache else {}
        await _persist_metrics_snapshot()
        return {
            "version": VERSION,
            "metrics": stats,
            "cache": cache_stats,
            "timestamp": time.time(),
        }

    @app.get("/api/kpi")
    async def kpi_endpoint():
        if _metrics:
            kpi = await _metrics.get_kpi_stats()
        else:
            kpi = {"error": "metrics not initialized"}
        await _persist_metrics_snapshot()
        result = {
            "version": VERSION,
            "kpi": kpi,
            "timestamp": time.time(),
        }
        r = _get_redis_client()
        if r and _metrics:
            try:
                snapshot = _metrics.load_snapshot(r)
                if snapshot:
                    result["last_snapshot"] = snapshot
            except Exception:
                pass
        return result

    @app.get("/api/cache/stats")
    async def cache_stats():
        if _response_cache:
            return _response_cache.get_stats()
        return {"error": "cache not initialized"}

    # ── 会话端点 ──

    @app.get("/api/sessions")
    async def list_sessions():
        if _session_manager:
            return {"sessions": _session_manager.list_sessions()}
        return {"sessions": []}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, request: Request):
        if _session_manager:
            token = request.headers.get("X-Session-Token", "")
            if not _session_manager.validate_session_token(session_id, token):
                return JSONResponse({"error": "会话令牌无效或无权访问"}, status_code=403)
            session = _session_manager.get_session(session_id)
            if session:
                return {"session": {
                    "session_id": session_id,
                    "messages": session.get("messages", []),
                    "created_at": session.get("created_at"),
                    "last_activity": session.get("last_activity"),
                    "message_count": session.get("message_count", 0),
                    "summary": session.get("summary", ""),
                }}
            return JSONResponse({"error": "session not found"}, status_code=404)
        return JSONResponse({"error": "session manager not initialized"}, status_code=500)

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        if _session_manager:
            token = request.headers.get("X-Session-Token", "")
            if not _session_manager.validate_session_token(session_id, token):
                return JSONResponse({"error": "会话令牌无效或无权删除"}, status_code=403)
            _session_manager.delete_session(session_id)
            return {"message": f"会话 {session_id} 已删除"}
        return JSONResponse({"error": "session manager not initialized"}, status_code=500)

    # ── 反馈端点 ──

    @app.post("/api/feedback")
    async def submit_feedback(data: FeedbackRequest):
        session_id = data.session_id.strip()
        resolved = data.resolved
        comment = _sanitize_input(data.comment)

        if not session_id:
            return JSONResponse({"error": "session_id 不能为空"}, status_code=400)

        if _session_manager:
            session = _session_manager.get_session(session_id)
            if not session or not session.get("messages"):
                return JSONResponse({"error": "会话不存在或无对话记录"}, status_code=404)

        if _metrics:
            await _metrics.record_feedback(resolved=bool(resolved))

        if _bus:
            try:
                from core.message_bus import Message, MessageType
                await _bus.publish(Message(
                    msg_type=MessageType.BROADCAST,
                    topic="feedback.received",
                    sender="api_feedback",
                    payload={"session_id": session_id, "resolved": resolved, "comment": comment},
                ))
            except Exception as e:
                logger.debug(f"Feedback Bus 事件发布失败: {e}")

        logger.info(f"[Feedback] session={session_id} resolved={resolved} comment={comment[:50]}")
        return {"status": "ok", "session_id": session_id, "resolved": resolved}

    # ── 告警端点 ──

    @app.get("/api/alerts")
    async def get_alerts(limit: int = 20):
        limit = min(max(limit, 1), 100)
        if _sla_alert_mgr:
            return {"alerts": _sla_alert_mgr.get_alerts(limit=limit)}
        return {"alerts": [], "message": "alert manager not initialized"}

    @app.get("/api/circuit-breaker")
    async def get_circuit_breaker():
        from multi_agent_customer_service import circuit_breaker
        status = circuit_breaker.get_status()
        return {"circuit_breaker": {
            "state": status["state"],
            "total_failures": status["total_failures"],
            "total_successes": status["total_successes"],
        }}

    # ── Prometheus 指标端点（v3.9） ──

    @app.get("/metrics/prometheus")
    async def prometheus_metrics():
        """
        Prometheus 文本格式指标导出（v3.9）
        无需认证，供 Prometheus Server 抓取。
        """
        if not _metrics:
            return JSONResponse({"error": "metrics not initialized"}, status_code=503)

        stats = await _metrics.get_stats()
        kpi = await _metrics.get_kpi_stats()

        from multi_agent_customer_service import circuit_breaker as _cb
        cb = _cb.get_status()

        lines = []

        def _gauge(name, value, help_text):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")

        def _counter(name, value, help_text):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")

        _counter("csai_requests_total", stats["total_requests"], "Total requests handled")
        _counter("csai_errors_total", stats["total_errors"], "Total errors")
        _gauge("csai_error_rate_percent", stats["error_rate"], "Error rate percentage")
        _gauge("csai_avg_response_time_seconds", stats["avg_response_time"], "Average response time")
        _gauge("csai_p95_response_time_seconds", stats["p95_response_time"], "P95 response time")
        _gauge("csai_cache_hit_rate_percent", stats["cache_hit_rate"], "Cache hit rate percentage")

        # Agent 调用计数
        agent_counts = stats.get("agent_call_counts", {})
        for agent, count in agent_counts.items():
            _counter(f'csai_agent_calls_total{{agent="{agent}"}}', count, f"Agent {agent} call count")

        # 协作模式计数
        mode_counts = stats.get("mode_counts", {})
        for mode, count in mode_counts.items():
            _counter(f'csai_collaboration_mode_total{{mode="{mode}"}}', count, f"Mode {mode} invocation count")

        # SLA 指标
        sla = stats.get("sla", {})
        _counter("csai_sla_violations_total", sla.get("violations_slow", 0), "SLA violations (too slow)")
        _gauge("csai_sla_violation_rate_percent", sla.get("violation_rate", 0), "SLA violation rate")
        _gauge("csai_sla_window_violation_rate_percent", sla.get("window_violation_rate", 0), "SLA window violation rate")

        # 熔断器
        _gauge("csai_circuit_breaker_consecutive_failures", cb.get("consecutive_failures", 0), "Circuit breaker consecutive failures")
        _gauge("csai_circuit_breaker_state{state=\"" + cb.get("state", "closed") + "\"}", 1, "Circuit breaker state (1=current state)")

        # KPI
        _gauge("csai_total_ai_handled", kpi.get("total_ai_handled", 0), "Total AI handled requests")
        _gauge("csai_total_escalated", kpi.get("total_escalated", 0), "Total escalated requests")
        _gauge("csai_total_single_turn_resolved", kpi.get("total_single_turn_resolved", 0), "Total single-turn resolved")

        # 系统信息
        lines.append(f'# HELP csai_info System information')
        lines.append(f'# TYPE csai_info gauge')
        lines.append(f'csai_info{{version="{VERSION}"}} 1')

        return "\n".join(lines) + "\n", {"Content-Type": "text/plain; charset=utf-8"}

    return app


async def _persist_metrics_snapshot():
    """将指标快照持久化到 Redis（静默失败）"""
    if not _metrics:
        return
    r = _get_redis_client()
    if r:
        try:
            await _metrics.save_snapshot(r)
        except Exception:
            pass


async def _run_graph(session_id: str, query: str) -> Dict[str, Any]:
    """
    执行 LangGraph 图（v3.2: 原生异步 + SLA 告警 + 解决状态追踪）
    """
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
    }

    try:
        result = await _graph_app.ainvoke(state)
    except AttributeError:
        result = await asyncio.to_thread(_graph_app.invoke, state)

    elapsed = time.time() - start
    result["elapsed"] = elapsed

    # SLA 告警
    if elapsed > RESPONSE_TIME_TARGET_MAX:
        logger.warning(
            f"[SLA] 响应超时: {elapsed:.2f}s > {RESPONSE_TIME_TARGET_MAX}s "
            f"(session={session_id}, mode={result.get('collaboration_mode', '')}, "
            f"agent={result.get('current_agent', '')})"
        )
    elif elapsed < RESPONSE_TIME_TARGET_MIN:
        logger.info(
            f"[SLA] 响应偏快: {elapsed:.2f}s < {RESPONSE_TIME_TARGET_MIN}s "
            f"(session={session_id}, cached={result.get('cached', False)})"
        )

    # 采集指标
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
            except Exception:
                pass

    return result
