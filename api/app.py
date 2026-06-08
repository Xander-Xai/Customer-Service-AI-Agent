"""
FastAPI + WebSocket 异步服务层 (v4.0 — 用户认证 + 知识库管理)
核心改造：
- v4.0: 集成 JWT 用户认证 + 知识库管理 + 告警通知路由
- v4.0: 双认证模式（API Key 系统间 + JWT 终端用户）
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
import base64
import hmac
import json
import os
import re
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, Any
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, File, UploadFile
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
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
    DEV_MODE,
    LLM_PROVIDER,
    MULTIMODAL_ENABLED, MAX_IMAGE_SIZE_MB, ALLOWED_IMAGE_TYPES,
)
from logger import get_logger, set_trace_id, get_trace_id
from auth.service import decode_token as _decode_jwt_token

logger = get_logger("api")

# ── 模块加载时间（用于 uptime 计算）──
_MODULE_LOAD_TIME = time.time()

# ── 输入净化 ──
_CONTROL_CHAR_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_HTML_TAG_RE = re.compile(r'<[^>]+>')


def _sanitize_input(text: str) -> str:
    """净化用户输入：移除控制字符和 HTML 标签（v4.0: 增加 HTML 实体解码防 XSS）"""
    import html as _html_mod
    text = _CONTROL_CHAR_RE.sub('', text)
    # v4.0: 先解码 HTML 实体再移除标签，防止 &lt;script&gt; 绕过
    text = _html_mod.unescape(text)
    text = _HTML_TAG_RE.sub('', text)
    return text.strip()


# ── Session ID 校验 ──
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _validate_session_id(sid: str) -> str:
    """校验并清理 session_id，不合法则自动生成 UUID"""
    if sid and _SESSION_ID_RE.match(sid):
        return sid
    return str(uuid.uuid4())


# ── 认证辅助函数（v4.1: 消除 auth_middleware 重复代码）──

def _check_api_key(request) -> bool:
    """检查 API Key 认证"""
    api_key = request.headers.get("X-API-Key", "")
    return API_KEY_ENABLED and API_KEY and hmac.compare_digest(api_key, API_KEY)


def _check_jwt_auth(request) -> dict | None:
    """检查 JWT 认证，返回 payload 或 None"""
    auth_header = request.headers.get("Authorization", "")
    jwt_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if jwt_token:
        return _decode_jwt_token(jwt_token)
    return None


def _extract_user_id(request) -> str | None:
    """从请求中提取当前用户 ID（JWT sub 字段）"""
    payload = _check_jwt_auth(request)
    if payload:
        return payload.get("sub")
    return None


def _check_admin_token(request) -> bool:
    """检查 Admin Token"""
    admin_token = request.headers.get("X-Admin-Token", "")
    return MONITORING_ADMIN_TOKEN and hmac.compare_digest(admin_token, MONITORING_ADMIN_TOKEN)


def _is_authenticated(request) -> bool:
    """综合认证检查：API Key 或 JWT"""
    if _check_api_key(request):
        return True
    if _check_jwt_auth(request):
        return True
    return False


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
_ws_conn_counter = 0  # 用于触发周期性清理


def _cleanup_stale_ws_connections():
    """清理 _ws_connections 中连接数为 0 的条目，防止内存泄漏"""
    stale_keys = [ip for ip, count in _ws_connections.items() if count <= 0]
    for key in stale_keys:
        del _ws_connections[key]
    if stale_keys:
        logger.info(f"[WS] 清理 {len(stale_keys)} 个过期连接记录")
    return len(stale_keys)


# ── Pydantic 模型 ──
class ChatRequest(BaseModel):
    query: str = Field(..., max_length=2000)
    session_id: str = Field(default="", max_length=36)
    session_token: str = Field(default="", max_length=64)


class ChatStreamRequest(BaseModel):
    """SSE 流式输出请求模型（字段与 ChatRequest 一致）"""
    query: str = Field(..., max_length=2000)
    session_id: str = Field(default="", max_length=36)
    session_token: str = Field(default="", max_length=64)


class FeedbackRequest(BaseModel):
    session_id: str = Field(..., max_length=36)
    resolved: bool = Field(default=True)
    rating: int = Field(default=1, ge=-1, le=1)  # 1=点赞, -1=点踩
    message_index: int = Field(default=0, ge=0)  # 第几条回复
    comment: str = Field(default="", max_length=500)


# ── 全局引用（在 create_app 中注入） ──
_graph_app = None
_session_manager = None
_response_cache = None
_metrics = None
_bus = None
_sla_alert_mgr = None
_redis_client = None
_circuit_breaker_ref = None  # P0-1: 从容器注入，消除 multi_agent_customer_service 依赖


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


def _redis_rate_limit(client_ip: str, max_requests: int, window_seconds: int) -> bool:
    """Redis 滑动窗口限流，返回 True 表示允许"""
    r = _get_redis_client()
    if not r:
        return False  # Redis 不可用，回退到内存限流
    try:
        now = time.time()
        key = f"csai:rate:{client_ip}"
        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, now - window_seconds)  # 清除过期
        pipe.zadd(key, {str(now): now})  # 添加当前请求
        pipe.expire(key, window_seconds)  # 设置过期
        result = pipe.execute()
        count = result[1]  # zadd 返回新增数量
        return count <= max_requests
    except Exception:
        return False  # Redis 异常，回退到允许


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

        # v4.4: 周期性清理过期的 WebSocket 连接记录（每 5 分钟）
        async def _periodic_ws_cleanup():
            while True:
                await asyncio.sleep(300)  # 5 分钟
                try:
                    async with _ws_lock:
                        cleaned = _cleanup_stale_ws_connections()
                    if cleaned:
                        logger.debug(f"[WS] 周期性清理: 移除 {cleaned} 个过期条目")
                except Exception as e:
                    logger.warning(f"[WS] 周期性清理异常: {e}")

        cleanup_task = asyncio.create_task(_periodic_ws_cleanup())

        yield

        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

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
        allow_methods=["GET", "POST", "DELETE", "PUT"],
        allow_headers=["X-API-Key", "X-Admin-Token", "X-Session-Token", "Content-Type", "Authorization"],
    )

    # ── 限流中间件 ──
    _rate_limit_store: Dict[str, list] = defaultdict(list)
    _RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "60"))
    _RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))
    _rate_limit_cleanup_counter = 0

    def _cleanup_rate_limit_store():
        """定期清理过期的限流记录，防止内存无限增长"""
        now = time.time()
        expired_keys = [
            key for key, timestamps in _rate_limit_store.items()
            if not timestamps or now - timestamps[-1] > 3600
        ]
        for key in expired_keys:
            del _rate_limit_store[key]

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if request.url.path in ("/", "/api/health") or request.url.path.startswith("/static/") or request.url.path.startswith("/ws/"):
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # v4.0: 每 1000 次请求清理一次过期记录
        nonlocal _rate_limit_cleanup_counter
        _rate_limit_cleanup_counter += 1
        if _rate_limit_cleanup_counter >= 1000:
            _rate_limit_cleanup_counter = 0
            _cleanup_rate_limit_store()

        # v4.0: 认证端点独立限流（登录 5次/5分钟，注册 3次/小时）
        auth_path = request.url.path
        if auth_path == "/api/auth/login":
            auth_key = f"auth_login:{client_ip}"
            _rate_limit_store[auth_key] = [t for t in _rate_limit_store.get(auth_key, []) if now - t < 300]
            if len(_rate_limit_store[auth_key]) >= 5:
                return JSONResponse({"error": "登录尝试过于频繁，请 5 分钟后重试"}, status_code=429)
            _rate_limit_store[auth_key].append(now)
            return await call_next(request)
        if auth_path == "/api/auth/register":
            auth_key = f"auth_register:{client_ip}"
            _rate_limit_store[auth_key] = [t for t in _rate_limit_store.get(auth_key, []) if now - t < 3600]
            if len(_rate_limit_store[auth_key]) >= 3:
                return JSONResponse({"error": "注册过于频繁，请稍后再试"}, status_code=429)
            _rate_limit_store[auth_key].append(now)
            return await call_next(request)
        # 优先 Redis 限流（滑动窗口）
        if _get_redis_client():
            if not _redis_rate_limit(client_ip, _RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW):
                return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
            return await call_next(request)

        # 回退到内存限流（原有逻辑）
        _rate_limit_store[client_ip] = [t for t in _rate_limit_store[client_ip] if now - t < _RATE_LIMIT_WINDOW]
        if len(_rate_limit_store[client_ip]) >= _RATE_LIMIT_MAX:
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        _rate_limit_store[client_ip].append(now)
        return await call_next(request)

    # ── 安全响应头 ──
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # M3: 为每个请求生成 CSP nonce
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' 'unsafe-hashes'; "
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
    async def serve_index(request: Request):
        index_path = os.path.join(templates_dir, "index.html")
        if os.path.exists(index_path):
            nonce = getattr(request.state, "csp_nonce", "")
            with open(index_path, "r", encoding="utf-8") as f:
                content = f.read()
            # M3: 注入 CSP nonce 到 script 标签
            if nonce:
                content = content.replace("<script", f'<script nonce="{nonce}"')
            return HTMLResponse(content=content)
        return HTMLResponse("<h1>前端文件未找到</h1>", status_code=404)

    # v4.1: 登录页面路由
    @app.get("/login.html", response_class=HTMLResponse)
    async def serve_login(request: Request):
        login_path = os.path.join(templates_dir, "login.html")
        if os.path.exists(login_path):
            nonce = getattr(request.state, "csp_nonce", "")
            with open(login_path, "r", encoding="utf-8") as f:
                content = f.read()
            if nonce:
                content = content.replace("<script", f'<script nonce="{nonce}"')
            return HTMLResponse(content=content)
        return HTMLResponse("<h1>登录页面未找到</h1>", status_code=404)

    # v4.1: 管理后台路由
    @app.get("/admin.html", response_class=HTMLResponse)
    async def serve_admin(request: Request):
        admin_path = os.path.join(templates_dir, "admin.html")
        if os.path.exists(admin_path):
            nonce = getattr(request.state, "csp_nonce", "")
            with open(admin_path, "r", encoding="utf-8") as f:
                content = f.read()
            if nonce:
                content = content.replace("<script", f'<script nonce="{nonce}"')
            return HTMLResponse(content=content)
        return HTMLResponse("<h1>管理后台未找到</h1>", status_code=404)

    # ── API Key 认证中间件（v4.1: 配置驱动，消除重复代码）──
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # 公开路径
        if path in ("/", "/api/health", "/login.html", "/admin.html") or path.startswith("/static/"):
            return await call_next(request)

        # 认证相关端点（无需认证）
        if path.startswith("/api/auth/login") or path.startswith("/api/auth/register"):
            return await call_next(request)

        # 确定所需认证级别
        required_auth = "api_key_or_jwt"  # 默认
        if path in ("/api/metrics", "/api/kpi", "/api/circuit-breaker", "/api/cache/stats", "/metrics/prometheus"):
            required_auth = "admin"
        elif path.startswith("/api/alerts") or path.startswith("/api/knowledge"):
            required_auth = "api_key_or_admin"

        # 检查认证
        if required_auth == "admin":
            # 监控端点始终需要认证，DEV_MODE 不跳过
            if _check_admin_token(request) or _check_api_key(request):
                return await call_next(request)
            payload = _check_jwt_auth(request)
            if payload and payload.get("role") == "admin":
                return await call_next(request)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        if required_auth == "api_key_or_admin":
            if _check_api_key(request) or _check_admin_token(request):
                return await call_next(request)
            # 回退到 JWT
            payload = _check_jwt_auth(request)
            if payload:
                return await call_next(request)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # 默认: api_key_or_jwt（DEV_MODE 可跳过）
        if DEV_MODE:
            # v4.3 安全加固：DEV_MODE 绕过认证时记录警告
            logger.warning(f"[SECURITY] DEV_MODE bypasses authentication for {request.url.path}")
            return await call_next(request)

        if _is_authenticated(request):
            return await call_next(request)

        return JSONResponse({"error": "Unauthorized: Invalid API Key or Token"}, status_code=401)

    # ── 分布式追踪中间件（v4.1: 每请求生成 trace_id，注入 contextvars + 响应头）──
    # 必须在 auth_middleware 之后注册，确保作为最外层中间件最先执行
    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace_id = str(uuid.uuid4())[:12]
        set_trace_id(trace_id)
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response

    # ── WebSocket 实时对话 ──

    @app.websocket("/ws/chat")
    async def websocket_chat(ws: WebSocket):
        ws_api_key = ws.query_params.get("api_key", "") or ws.headers.get("x-api-key", "")

        # v4.0 安全修复: 移除 localhost 绕过，改用 DEV_MODE 环境变量
        if API_KEY_ENABLED:
            authenticated = False
            if ws_api_key and hmac.compare_digest(ws_api_key, API_KEY):
                authenticated = True
            elif DEV_MODE:
                authenticated = True

            if not authenticated:
                await ws.close(code=4001, reason="Unauthorized")
                return

        client_ip = ws.client.host if ws.client else "unknown"
        async with _ws_lock:
            if _ws_connections[client_ip] >= WS_MAX_CONNECTIONS_PER_IP:
                await ws.close(code=4029, reason="Too many connections")
                return
            _ws_connections[client_ip] += 1

        await ws.accept()

        # v4.3 安全加固: 从首条消息获取 JWT Token（而非 URL 参数，避免 token 泄露到日志）
        session_token = ""
        ws_jwt_payload = None
        if API_KEY_ENABLED and not DEV_MODE and not ws_api_key:
            try:
                # 等待客户端发送认证消息（10秒超时）
                auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
                ws_jwt = auth_msg.get("token", "")
                session_token = auth_msg.get("session_token", "")
                if not ws_jwt:
                    await ws.send_json({"type": "error", "message": "认证失败: 缺少 token"})
                    await ws.close(code=4001, reason="Missing token")
                    return
                from auth.service import decode_token
                payload = decode_token(ws_jwt)
                if not payload:
                    await ws.send_json({"type": "error", "message": "认证失败: 无效的 token"})
                    await ws.close(code=4001, reason="Invalid token")
                    return
                ws_jwt_payload = payload
            except asyncio.TimeoutError:
                await ws.send_json({"type": "error", "message": "认证超时"})
                await ws.close(code=4002, reason="Auth timeout")
                return
            except Exception:
                await ws.send_json({"type": "error", "message": "认证失败"})
                await ws.close(code=4003, reason="Auth error")
                return

        session_id = str(uuid.uuid4())
        if _session_manager and not session_token:
            session_token = _session_manager.generate_session_token(session_id)
        logger.info(f"[WS] 新连接: {session_id} ip={client_ip}")

        # H-3: WS 连接设置 user_id（从 JWT payload 中提取）
        if _session_manager and ws_jwt_payload:
            ws_uid = ws_jwt_payload.get("sub", "")
            if ws_uid:
                _session_manager.set_user_id(session_id, ws_uid)

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
                    # v4.0 安全修复: 移除 localhost 绕过，统一使用 DEV_MODE
                    if not DEV_MODE:
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
                # v4.4: 每 50 次连接事件触发一次清理（补充周期性清理）
                global _ws_conn_counter
                _ws_conn_counter += 1
                if _ws_conn_counter >= 50:
                    _ws_conn_counter = 0
                    _cleanup_stale_ws_connections()
            if _bus:
                try:
                    await _bus.unsubscribe("agent.processing", on_agent_event)
                    await _bus.unsubscribe("agent.completed", on_agent_event)
                except Exception as e:
                    logger.warning(f"[WS] Bus 取消订阅失败 session={session_id}: {e}")

    # ── REST API ──

    @app.post("/api/chat")
    async def rest_chat(data: ChatRequest, request: Request):
        query = _sanitize_input(data.query)[:MAX_QUERY_LENGTH]
        sid = _validate_session_id(data.session_id)
        if not query:
            return JSONResponse({"error": "query 不能为空"}, status_code=400)
        client_provided_sid = bool(data.session_id)
        if client_provided_sid and _session_manager:
            if not _session_manager.validate_session_token(sid, data.session_token or ""):
                return JSONResponse({"error": "会话令牌无效"}, status_code=403)
        try:
            # H-3: 设置会话用户 ID
            user_id = _extract_user_id(request)
            if _session_manager and user_id:
                _session_manager.set_user_id(sid, user_id)
            result = await _run_graph(sid, query)
            if _session_manager and not client_provided_sid:
                result["session_token"] = _session_manager.generate_session_token(sid)
            return result
        except Exception as e:
            logger.error(f"REST 处理失败: {e}", exc_info=True)
            return JSONResponse({"error": "服务内部错误，请稍后重试"}, status_code=500)

    # ── SSE 流式输出端点（v4.2: 真流式 — LLM stream=True） ──

    @app.post("/api/chat/stream")
    async def stream_chat(data: ChatStreamRequest, request: Request):
        """
        SSE 真流式输出端点（v4.2）
        LLM 使用 stream=True 逐 token 推送，前端实时渲染。
        流式阶段：status -> progress -> chunk*（真流式）-> done | error
        """
        query = _sanitize_input(data.query)[:MAX_QUERY_LENGTH]
        sid = _validate_session_id(data.session_id)
        if not query:
            return JSONResponse({"error": "query 不能为空"}, status_code=400)

        # 会话令牌校验
        client_provided_sid = bool(data.session_id)
        if client_provided_sid and _session_manager:
            if not _session_manager.validate_session_token(sid, data.session_token or ""):
                return JSONResponse({"error": "会话令牌无效"}, status_code=403)

        # H-3: 设置会话用户 ID
        user_id = _extract_user_id(request)
        if _session_manager and user_id:
            _session_manager.set_user_id(sid, user_id)

        # 流式通信队列（Agent → SSE Generator）
        chunk_queue: asyncio.Queue = asyncio.Queue()

        async def stream_callback(event: dict):
            """Agent 调用此回调推送 chunk；None 表示流结束"""
            await chunk_queue.put(event)

        # 在后台运行图执行（含流式回调）
        graph_task = asyncio.create_task(
            _run_graph_stream(sid, query, stream_callback)
        )

        def _sse_event(event_data: dict) -> str:
            return f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"

        async def _event_generator():
            start_time = time.time()
            try:
                yield _sse_event({"type": "status", "content": "正在分析您的问题..."})

                # 等待图开始产生输出（progress 阶段由图内部处理）
                yield _sse_event({"type": "progress", "content": "正在识别意图..."})

                # 从队列读取真流式 chunk
                # 缓存命中时图可能已经完成，先检查
                if not graph_task.done():
                    while True:
                        try:
                            event = await asyncio.wait_for(chunk_queue.get(), timeout=60.0)
                        except asyncio.TimeoutError:
                            if graph_task.done():
                                break
                            continue

                        if event is None:  # 哨兵：流结束
                            break
                        if isinstance(event, dict) and event.get("type") == "chunk":
                            yield _sse_event(event)

                # 获取图执行结果（已完成）
                try:
                    result = await graph_task
                except Exception as e:
                    logger.error(f"SSE 图执行失败: {e}")
                    yield _sse_event({"type": "error", "content": "服务内部错误，请稍后重试"})
                    return

                # 生成 session_token
                session_token = ""
                if _session_manager and not client_provided_sid:
                    session_token = _session_manager.generate_session_token(sid)

                elapsed = round(time.time() - start_time, 3)
                yield _sse_event({
                    "type": "done",
                    "agent": result.get("current_agent", ""),
                    "mode": result.get("collaboration_mode", "sequential"),
                    "elapsed": elapsed,
                    "cached": result.get("cached", False),
                    "agents_used": result.get("agents_used", []),
                    "resolution_status": result.get("resolution_status", ""),
                    "session_id": sid,
                    "session_token": session_token,
                })

            except Exception as e:
                logger.error(f"SSE 流式处理失败: {e}", exc_info=True)
                yield _sse_event({"type": "error", "content": "服务内部错误，请稍后重试"})

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── 多模态对话端点（v4.1: 图片 + 文字） ──

    @app.post("/api/chat/image")
    async def chat_with_image(
        request: Request,
        query: str = Form(""),
        image: UploadFile = File(...),
        session_id: str = Form(""),
        session_token: str = Form(""),
    ):
        """
        多模态对话：图片 + 文字
        - 读取上传的图片，转为 base64
        - 构造多模态消息（text + image_url）
        - 调用支持多模态的 LLM
        """
        # 1. 多模态功能开关检查
        if not MULTIMODAL_ENABLED:
            return JSONResponse(
                {"error": "多模态功能未启用，请在配置中设置 MULTIMODAL_ENABLED=true"},
                status_code=400,
            )

        # 2. 输入清理
        query = _sanitize_input(query)[:MAX_QUERY_LENGTH]
        sid = _validate_session_id(session_id)

        # 3. 图片 MIME 类型验证
        content_type = image.content_type or ""
        if content_type not in ALLOWED_IMAGE_TYPES:
            return JSONResponse(
                {"error": f"不支持的图片类型: {content_type}，允许的类型: {', '.join(ALLOWED_IMAGE_TYPES)}"},
                status_code=400,
            )

        # 4. 读取并限制图片大小
        try:
            image_bytes = await image.read()
        except Exception as e:
            logger.error(f"图片读取失败: {e}")
            return JSONResponse({"error": "图片读取失败，请重新上传"}, status_code=400)

        max_bytes = MAX_IMAGE_SIZE_MB * 1024 * 1024
        if len(image_bytes) > max_bytes:
            return JSONResponse(
                {"error": f"图片大小超过限制（最大 {MAX_IMAGE_SIZE_MB}MB）"},
                status_code=400,
            )

        if len(image_bytes) == 0:
            return JSONResponse({"error": "上传的图片文件为空"}, status_code=400)

        # 5. 转为 base64（内存保护：限制 base64 字符串长度）
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        # base64 编码后约为原始大小的 1.33 倍，设上限为 15MB 字符串
        if len(image_b64) > 15 * 1024 * 1024:
            return JSONResponse({"error": "图片编码后数据过大"}, status_code=400)

        data_url = f"data:{content_type};base64,{image_b64}"

        # 6. 构造多模态消息
        user_text = query if query else "请分析这张图片"
        multimodal_messages = [
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}
        ]

        # 7. 会话令牌校验（v4.1 安全修复: 从 Form 字段获取 session_token，不再跳过校验）
        client_provided_sid = bool(session_id)
        if client_provided_sid and _session_manager:
            if not _session_manager.validate_session_token(sid, session_token or ""):
                return JSONResponse({"error": "会话令牌无效"}, status_code=403)

        # H-3: 设置会话用户 ID
        uid = _extract_user_id(request)
        if _session_manager and uid:
            _session_manager.set_user_id(sid, uid)

        # 8. 调用 LLM（通过 OpenAI 兼容客户端）
        try:
            from core.monitoring import OpenAICompatibleClient
            import config as _cfg

            # P0-1: 从容器获取 circuit_breaker，消除 multi_agent_customer_service 依赖
            _container = getattr(request.app.state, "container", None)
            _cb = _container.circuit_breaker if _container and _container.circuit_breaker else (
                _circuit_breaker_ref
            )

            client = OpenAICompatibleClient(
                api_key=_cfg.OPENAI_API_KEY,
                base_url=_cfg.OPENAI_BASE_URL,
                model=_cfg.OPENAI_MODEL,
                circuit_breaker=_cb,
            )

            # 直接构造 OpenAI 格式的 messages，绕过 _format_messages（因为它会转换 content 列表为字符串）
            payload = {
                "model": client.model,
                "messages": multimodal_messages,
            }
            http_client = await client._get_async_client()
            resp = await http_client.post(
                f"{client.base_url}/chat/completions",
                json=payload,
                headers=client.headers,
                timeout=httpx.Timeout(client.timeout),
            )
            resp.raise_for_status()
            result = resp.json()

            if "choices" in result and result["choices"]:
                content = result["choices"][0].get("message", {}).get("content", "") or ""
            else:
                content = "抱歉，无法分析该图片，请稍后重试。"

            if client.circuit_breaker:
                await client.circuit_breaker.record_success()

        except Exception as e:
            logger.error(f"多模态 LLM 调用失败: {e}", exc_info=True)
            content = "图片分析失败，请稍后重试"

        # 9. 返回结果
        session_token = ""
        if _session_manager and not client_provided_sid:
            session_token = _session_manager.generate_session_token(sid)

        return {
            "response": content,
            "session_id": sid,
            "session_token": session_token,
            "elapsed": 0,
        }

    # ── 健康检查（P0-1: 从 app.state.container 获取服务） ──

    @app.get("/api/health")
    async def health(request: Request):
        """
        增强健康检查端点（v4.2）
        返回各子系统的健康状态、版本和运行信息。
        状态逻辑：
          - healthy: 所有关键组件（DB/LLM）正常
          - degraded: 非关键组件（Redis/ChromaDB）异常但核心功能可用
          - unhealthy: 数据库或 LLM 不可用
        """
        # P0-1: 优先从容器获取 circuit_breaker，回退到模块级变量
        container = getattr(request.app.state, "container", None)
        if container and container.circuit_breaker:
            cb_status = container.circuit_breaker.get_status()
        elif _circuit_breaker_ref is not None:
            cb_status = _circuit_breaker_ref.get_status()
        else:
            cb_status = {"state": "unknown", "consecutive_failures": 0}

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

        # LLM 可达性检查（v4.1: 检查 API Key 是否有效，非占位符）
        llm_api_key = os.environ.get("OPENAI_API_KEY", "")
        _placeholder_prefixes = ("sk-placeholder", "your-", "sk-xxx", "sk-your", "sk-test-placeholder")
        llm_key_valid = bool(llm_api_key) and not any(
            llm_api_key.lower().startswith(p) for p in _placeholder_prefixes
        )
        llm_configured = bool(llm_api_key)
        llm_provider = LLM_PROVIDER

        # ChromaDB 检查
        chromadb_ok = False
        try:
            import chromadb
            client = chromadb.Client()
            client.heartbeat()
            chromadb_ok = True
        except Exception:
            chromadb_ok = False

        # 数据库检查（SELECT 1 延迟测试）
        db_ok = False
        db_latency_ms = None
        try:
            from db.database import engine
            from sqlalchemy import text as _sql_text
            t0 = time.time()
            with engine.connect() as conn:
                conn.execute(_sql_text("SELECT 1"))
            db_latency_ms = round((time.time() - t0) * 1000, 2)
            db_ok = True
        except Exception:
            db_ok = False

        # 运行时间
        uptime_seconds = round(time.time() - _MODULE_LOAD_TIME, 2)

        # 系统状态汇总
        circuit_state = cb_status["state"]
        overall = "healthy"

        # 关键组件不可用 → unhealthy
        if not db_ok or not llm_key_valid:
            overall = "unhealthy"
        # 非关键组件异常但核心可用 → degraded
        elif circuit_state == "open" or (not redis_ok and REDIS_URL) or not chromadb_ok:
            overall = "degraded"

        return {
            "status": overall,
            "version": VERSION,
            "mode": "dev" if DEV_MODE else "prod",
            "timestamp": time.time(),
            "uptime_seconds": uptime_seconds,
            "python_version": sys.version.split()[0],
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
                    "key_valid": llm_key_valid,
                    "provider": llm_provider,
                },
                "chromadb": {
                    "connected": chromadb_ok,
                },
                "database": {
                    "connected": db_ok,
                    "latency_ms": db_latency_ms,
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

    # H-3: 会话用户级隔离辅助函数
    def _check_session_ownership(session: dict, user_id: str | None) -> bool:
        """检查会话是否属于当前用户。DEV_MODE 或无 user_id 时放行。"""
        if DEV_MODE or not user_id:
            return True
        session_user = session.get("user_id")
        # 会话未关联用户（旧数据），放行以兼容
        if not session_user:
            return True
        return session_user == user_id

    # ── 会话端点 ──

    @app.get("/api/sessions")
    async def list_sessions(request: Request, offset: int = 0, limit: int = 20):
        if _session_manager:
            offset = max(offset, 0)
            limit = min(max(limit, 1), 100)
            result = await _session_manager.list_sessions_brief(offset=offset, limit=limit)
            # H-3: 用户级隔离 — 非 DEV_MODE 下只返回当前用户的会话
            user_id = _extract_user_id(request)
            if user_id and not DEV_MODE:
                filtered = [s for s in result.get("sessions", []) if s.get("user_id") == user_id]
                result["sessions"] = filtered
                result["total"] = len(filtered)
            return result
        return {"sessions": [], "total": 0, "offset": offset, "limit": limit}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, request: Request):
        if _session_manager:
            # v4.1: DEV_MODE 下跳过会话令牌校验
            if not DEV_MODE:
                token = request.headers.get("X-Session-Token", "")
                if not _session_manager.validate_session_token(session_id, token):
                    return JSONResponse({"error": "会话令牌无效或无权访问"}, status_code=403)
            session = await _session_manager.get_session(session_id)
            if session:
                # H-3: 用户级隔离 — 校验会话所有权
                user_id = _extract_user_id(request)
                if not _check_session_ownership(session, user_id):
                    return JSONResponse({"error": "无权访问该会话"}, status_code=403)
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
            # H-3: 用户级隔离 — 校验会话所有权
            user_id = _extract_user_id(request)
            session = await _session_manager.get_session(session_id)
            if session and not _check_session_ownership(session, user_id):
                return JSONResponse({"error": "无权删除该会话"}, status_code=403)
            if not DEV_MODE:
                token = request.headers.get("X-Session-Token", "")
                if not _session_manager.validate_session_token(session_id, token):
                    return JSONResponse({"error": "会话令牌无效或无权删除"}, status_code=403)
            await _session_manager.delete_session(session_id)
            return {"message": f"会话 {session_id} 已删除"}
        return JSONResponse({"error": "session manager not initialized"}, status_code=500)

    # ── D2: 历史会话端点 ──

    @app.get("/api/history")
    async def list_history(request: Request, offset: int = 0, limit: int = 20):
        """获取当前用户的历史会话列表（支持分页，使用批量摘要避免 N+1 查询）"""
        if not _session_manager:
            return {"sessions": [], "total": 0, "offset": offset, "limit": limit}
        offset = max(offset, 0)
        limit = min(max(limit, 1), 100)
        result = await _session_manager.list_sessions_brief(offset=offset, limit=limit)
        # H-3: 用户级隔离 — 非 DEV_MODE 下只返回当前用户的会话
        user_id = _extract_user_id(request)
        if user_id and not DEV_MODE:
            filtered = [s for s in result.get("sessions", []) if s.get("user_id") == user_id]
            result["sessions"] = filtered
            result["total"] = len(filtered)
        return result

    @app.get("/api/history/{session_id}/messages")
    async def get_history_messages(session_id: str, request: Request):
        """获取指定会话的消息历史（需要 session_token 验证）"""
        if not _session_manager:
            return JSONResponse({"error": "session manager not initialized"}, status_code=500)

        # H-3: 用户级隔离 — 校验会话所有权
        user_id = _extract_user_id(request)
        session = await _session_manager.get_session(session_id)
        if session and not _check_session_ownership(session, user_id):
            return JSONResponse({"error": "无权访问该会话"}, status_code=403)

        token = request.headers.get("X-Session-Token", "")
        if not _session_manager.validate_session_token(session_id, token):
            return JSONResponse({"error": "会话令牌无效或无权访问"}, status_code=403)

        if not session:
            return JSONResponse({"error": "会话不存在"}, status_code=404)

        messages = session.get("messages", [])
        return {"messages": [
            {"role": m.get("role", ""), "content": m.get("content", ""), "timestamp": m.get("timestamp", 0)}
            for m in messages
        ]}

    # ── 反馈端点 ──

    @app.post("/api/feedback")
    async def submit_feedback(data: FeedbackRequest):
        session_id = data.session_id.strip()
        resolved = data.resolved
        rating = data.rating
        message_index = data.message_index
        comment = _sanitize_input(data.comment)

        if not session_id:
            return JSONResponse({"error": "session_id 不能为空"}, status_code=400)

        if _session_manager:
            session = await _session_manager.get_session(session_id)
            if not session or not session.get("messages"):
                return JSONResponse({"error": "会话不存在或无对话记录"}, status_code=404)

        # D3: 持久化反馈到数据库
        try:
            from db.database import get_db_session
            from db.models import Feedback
            db = get_db_session()
            try:
                feedback = Feedback(
                    session_id=session_id,
                    message_index=message_index,
                    rating=rating,
                    comment=comment,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(feedback)
                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning(f"反馈数据库写入失败: {e}")
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"反馈数据库模块加载失败: {e}")

        if _metrics:
            await _metrics.record_feedback(resolved=bool(resolved))

        if _bus:
            try:
                from core.message_bus import Message, MessageType
                await _bus.publish(Message(
                    msg_type=MessageType.BROADCAST,
                    topic="feedback.received",
                    sender="api_feedback",
                    payload={"session_id": session_id, "resolved": resolved, "rating": rating, "comment": comment},
                ))
            except Exception as e:
                logger.debug(f"Feedback Bus 事件发布失败: {e}")

        logger.info(f"[Feedback] session={session_id} resolved={resolved} rating={rating} comment={comment[:50]}")
        return {"status": "ok", "session_id": session_id, "resolved": resolved, "rating": rating}

    @app.get("/api/feedback/stats")
    async def feedback_stats():
        """D3: 获取反馈统计数据"""
        try:
            from db.database import get_db_session
            from db.models import Feedback
            db = get_db_session()
            try:
                total = db.query(Feedback).count()
                positive = db.query(Feedback).filter(Feedback.rating == 1).count()
                negative = db.query(Feedback).filter(Feedback.rating == -1).count()
                rate = round(positive / total, 4) if total > 0 else 0.0
                return {"total": total, "positive": positive, "negative": negative, "rate": rate}
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"反馈统计查询失败: {e}")
            return {"total": 0, "positive": 0, "negative": 0, "rate": 0.0}

    # ── 告警端点 ──

    @app.get("/api/alerts")
    async def get_alerts(limit: int = 20):
        limit = min(max(limit, 1), 100)
        if _sla_alert_mgr:
            return {"alerts": _sla_alert_mgr.get_alerts(limit=limit)}
        return {"alerts": [], "message": "alert manager not initialized"}

    @app.get("/api/circuit-breaker")
    async def get_circuit_breaker(request: Request):
        # P0-1: 优先从容器获取 circuit_breaker
        container = getattr(request.app.state, "container", None)
        if container and container.circuit_breaker:
            status = container.circuit_breaker.get_status()
        elif _circuit_breaker_ref is not None:
            status = _circuit_breaker_ref.get_status()
        else:
            status = {"state": "unknown", "total_failures": 0, "total_successes": 0}
        return {"circuit_breaker": {
            "state": status["state"],
            "total_failures": status["total_failures"],
            "total_successes": status["total_successes"],
        }}

    # ── Prometheus 指标端点（v3.9） ──

    @app.get("/metrics/prometheus")
    async def prometheus_metrics(request: Request):
        """
        Prometheus 文本格式指标导出（v4.0: 内网访问限制）
        部署时应通过 Nginx 限制仅 Prometheus Server IP 可访问此端点。
        nginx.conf 中配置: allow 10.0.0.0/8; deny all;
        """
        if not _metrics:
            return JSONResponse({"error": "metrics not initialized"}, status_code=503)

        stats = await _metrics.get_stats()
        kpi = await _metrics.get_kpi_stats()

        # P0-1: 优先从容器获取 circuit_breaker
        container = getattr(request.app.state, "container", None)
        if container and container.circuit_breaker:
            cb = container.circuit_breaker.get_status()
        elif _circuit_breaker_ref is not None:
            cb = _circuit_breaker_ref.get_status()
        else:
            cb = {"state": "unknown", "consecutive_failures": 0}

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

        # v4.0 安全修复: 标签值转义（防止注入破坏 Prometheus 格式）
        _PROM_LABEL_RE = __import__("re").compile(r'[^a-zA-Z0-9_]')

        def _safe_label(v: str) -> str:
            return _PROM_LABEL_RE.sub("_", str(v))

        # Agent 调用计数
        agent_counts = stats.get("agent_call_counts", {})
        for agent, count in agent_counts.items():
            _counter(f'csai_agent_calls_total{{agent="{_safe_label(agent)}"}}', count, f"Agent {agent} call count")

        # 协作模式计数
        mode_counts = stats.get("mode_counts", {})
        for mode, count in mode_counts.items():
            _counter(f'csai_collaboration_mode_total{{mode="{_safe_label(mode)}"}}', count, f"Mode {mode} invocation count")

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
        "trace_id": get_trace_id(),
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


async def _run_graph_stream(session_id: str, query: str, stream_callback) -> Dict[str, Any]:
    """
    v4.2: 带流式回调的 LangGraph 图执行。
    将 stream_callback 注入到图状态中，Agent 层自动使用真流式 LLM 调用。
    执行完成后向队列发送结束信号。
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
        "trace_id": get_trace_id(),
        "stream_callback": stream_callback,  # v4.2: 流式回调注入
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
            f"[SLA-Stream] 响应超时: {elapsed:.2f}s > {RESPONSE_TIME_TARGET_MAX}s "
            f"(session={session_id}, mode={result.get('collaboration_mode', '')})"
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

    # 通知 SSE generator 流已结束
    if stream_callback:
        try:
            await stream_callback(None)  # 哨兵：流结束
        except Exception:
            pass

    return result
