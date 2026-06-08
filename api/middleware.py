"""
API 中间件栈：限流、安全头、认证、分布式追踪
从 api/app.py create_app() 提取。
"""

import hmac
import os
import secrets
import time
import uuid
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.utils import check_admin_token, check_api_key, check_jwt_auth, is_authenticated
from config import (
    DEV_MODE,
)
from logger import get_logger, set_trace_id

logger = get_logger("api.middleware")

# ── Redis 限流客户端（懒初始化）──
_redis_client = None


def get_redis_client():
    """获取 Redis 客户端单例（懒初始化，失败返回 None）"""
    global _redis_client
    if _redis_client is None:
        try:
            import redis

            from config import REDIS_URL

            _redis_client = redis.Redis.from_url(
                REDIS_URL, decode_responses=True, socket_connect_timeout=3
            )
            _redis_client.ping()
        except Exception as e:
            logger.debug(f"[Redis] 初始化失败: {e}")
            _redis_client = None
    return _redis_client


def _redis_rate_limit(client_ip: str, max_requests: int, window_seconds: int) -> bool:
    """Redis 滑动窗口限流，返回 True 表示允许"""
    r = get_redis_client()
    if not r:
        return False
    try:
        now = time.time()
        key = f"csai:rate:{client_ip}"
        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, window_seconds)
        result = pipe.execute()
        return result[1] <= max_requests
    except Exception as e:
        logger.debug(f"[Redis] 限流操作失败: {e}")
        return False


def setup_middleware(app: FastAPI):
    """注册所有 HTTP 中间件到 FastAPI 应用"""

    # ── 限流中间件 ──
    _rate_limit_store: dict[str, list] = defaultdict(list)
    _RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "60"))
    _RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))
    _rate_limit_cleanup_counter = 0

    def _cleanup_rate_limit_store():
        now = time.time()
        expired_keys = [
            key
            for key, timestamps in _rate_limit_store.items()
            if not timestamps or now - timestamps[-1] > 3600
        ]
        for key in expired_keys:
            del _rate_limit_store[key]

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if (
            request.url.path in ("/", "/api/health", "/login.html", "/admin.html", "/widget.html")
            or request.url.path.startswith("/static/")
            or request.url.path.startswith("/ws/")
        ):
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        nonlocal _rate_limit_cleanup_counter
        _rate_limit_cleanup_counter += 1
        if _rate_limit_cleanup_counter >= 1000:
            _rate_limit_cleanup_counter = 0
            _cleanup_rate_limit_store()

        auth_path = request.url.path
        if auth_path == "/api/auth/login":
            auth_key = f"auth_login:{client_ip}"
            _rate_limit_store[auth_key] = [
                t for t in _rate_limit_store.get(auth_key, []) if now - t < 300
            ]
            if len(_rate_limit_store[auth_key]) >= 5:
                return JSONResponse({"error": "登录尝试过于频繁，请 5 分钟后重试"}, status_code=429)
            _rate_limit_store[auth_key].append(now)
            return await call_next(request)
        if auth_path == "/api/auth/register":
            auth_key = f"auth_register:{client_ip}"
            _rate_limit_store[auth_key] = [
                t for t in _rate_limit_store.get(auth_key, []) if now - t < 3600
            ]
            if len(_rate_limit_store[auth_key]) >= 3:
                return JSONResponse({"error": "注册过于频繁，请稍后再试"}, status_code=429)
            _rate_limit_store[auth_key].append(now)
            return await call_next(request)

        # Redis 优先，失败则回退到内存限流
        redis_ok = _redis_rate_limit(client_ip, _RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW)
        if get_redis_client() and redis_ok:
            return await call_next(request)
        elif get_redis_client() and not redis_ok:
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)

        _rate_limit_store[client_ip] = [
            t for t in _rate_limit_store[client_ip] if now - t < _RATE_LIMIT_WINDOW
        ]
        if len(_rate_limit_store[client_ip]) >= _RATE_LIMIT_MAX:
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        _rate_limit_store[client_ip].append(now)
        return await call_next(request)

    # ── 安全响应头 ──
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
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
        response.headers["Permissions-Policy"] = "camera=(), geolocation=()"
        response.headers["X-XSS-Protection"] = "0"
        return response

    # ── API Key / JWT 认证中间件 ──
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # 输入大小保护：拒绝超过 1MB 的 POST 请求体
        if request.method == "POST" and path.startswith("/api/"):
            cl = request.headers.get("content-length")
            if cl and int(cl) > 1000000:
                return JSONResponse({"error": "Payload too large"}, status_code=413)

        if path in (
            "/",
            "/api/health",
            "/login.html",
            "/admin.html",
            "/widget.html",
        ) or path.startswith("/static/"):
            return await call_next(request)
        if path.startswith("/api/auth/login") or path.startswith("/api/auth/register"):
            return await call_next(request)

        required_auth = "api_key_or_jwt"
        if (
            path
            in (
                "/api/metrics",
                "/api/kpi",
                "/api/circuit-breaker",
                "/api/cache/stats",
                "/metrics/prometheus",
                "/api/feedback/stats",
            )
            or path.startswith("/api/alerts")
            or path.startswith("/api/monitoring")
        ):
            required_auth = "supervisor_or_admin"
        elif (
            path.startswith("/api/auth/users")
            or path.startswith("/api/auth/audit")
            or path.startswith("/api/knowledge")
        ):
            required_auth = "admin"

        if required_auth == "admin":
            if check_admin_token(request) or check_api_key(request):
                return await call_next(request)
            payload = check_jwt_auth(request)
            if payload and payload.get("role") == "admin":
                request.state.jwt_payload = payload
                return await call_next(request)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        if required_auth == "supervisor_or_admin":
            if check_admin_token(request) or check_api_key(request):
                return await call_next(request)
            payload = check_jwt_auth(request)
            if payload and payload.get("role") in ("admin", "supervisor"):
                request.state.jwt_payload = payload
                return await call_next(request)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        if DEV_MODE:
            client_ip = request.client.host if request.client else "unknown"
            logger.warning(
                f"[SECURITY] DEV_MODE auth bypass: {request.method} {request.url.path} from {client_ip}"
            )
            return await call_next(request)

        if is_authenticated(request):
            payload = check_jwt_auth(request)
            if payload:
                request.state.jwt_payload = payload
            return await call_next(request)

        return JSONResponse({"error": "Unauthorized: Invalid API Key or Token"}, status_code=401)

    # ── CSRF 双提交 Cookie 保护 ──
    _CSRF_COOKIE_NAME = "csrf_token"
    _CSRF_HEADER_NAME = "X-CSRF-Token"
    _CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
    # 浏览器请求特征：携带 Cookie 但无 Bearer Token
    _CSRF_SKIP_PATHS = {"/", "/api/health", "/login.html", "/admin.html", "/widget.html"}
    _CSRF_SKIP_PREFIXES = ("/static/", "/ws/")

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()

        # DEV_MODE 跳过 CSRF（与 auth_middleware 的 DEV_MODE bypass 一致）
        if DEV_MODE:
            return await call_next(request)

        # 安全方法、静态资源、WebSocket 跳过 CSRF
        if method in _CSRF_SAFE_METHODS or path in _CSRF_SKIP_PATHS:
            response = await call_next(request)
            # 在安全方法响应上设置 CSRF Cookie（无则生成）
            if method in _CSRF_SAFE_METHODS:
                existing = request.cookies.get(_CSRF_COOKIE_NAME)
                token = existing if existing else secrets.token_hex(32)
                response.set_cookie(
                    key=_CSRF_COOKIE_NAME,
                    value=token,
                    httponly=False,  # JS 需要读取
                    samesite="lax",
                    secure=not DEV_MODE,  # 生产环境强制 HTTPS
                    max_age=3600,
                    path="/",
                )
            return response

        if any(path.startswith(p) for p in _CSRF_SKIP_PREFIXES):
            return await call_next(request)

        # 携带 Bearer Token 的 API 客户端跳过 CSRF
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return await call_next(request)

        # 携带 API Key 或 Admin Token 的系统间调用跳过
        if request.headers.get("X-API-Key") or request.headers.get("X-Admin-Token"):
            return await call_next(request)

        # 浏览器请求验证 CSRF：Cookie 与 Header 必须匹配
        cookie_token = request.cookies.get(_CSRF_COOKIE_NAME)
        header_token = request.headers.get(_CSRF_HEADER_NAME)

        if not cookie_token or not header_token:
            client_ip = request.client.host if request.client else "unknown"
            logger.warning(
                f"[CSRF] Missing token: cookie={'yes' if cookie_token else 'no'}, header={'yes' if header_token else 'no'}, path={path}, ip={client_ip}"
            )
            return JSONResponse(
                {
                    "error": "CSRF validation failed: missing CSRF token. "
                    "Ensure the csrf_token cookie is set and the X-CSRF-Token header is included."
                },
                status_code=403,
            )

        if not hmac.compare_digest(cookie_token, header_token):
            client_ip = request.client.host if request.client else "unknown"
            logger.warning(f"[CSRF] Token mismatch: path={path}, ip={client_ip}")
            return JSONResponse(
                {"error": "CSRF validation failed: token mismatch"},
                status_code=403,
            )

        response = await call_next(request)
        # 刷新 Cookie 生命周期
        response.set_cookie(
            key=_CSRF_COOKIE_NAME,
            value=cookie_token,
            httponly=False,
            samesite="lax",
            secure=not DEV_MODE,
            max_age=3600,
            path="/",
        )
        return response

    # ── 分布式追踪中间件 ──
    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace_id = str(uuid.uuid4())[:12]
        set_trace_id(trace_id)
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response


# ── 角色权限映射 ──

ROLE_PERMISSIONS = {
    "customer": {"chat", "own_sessions", "feedback"},
    "agent": {"chat", "all_sessions", "feedback"},
    "supervisor": {"chat", "all_sessions", "feedback", "monitoring", "alerts"},
    "admin": {
        "chat",
        "all_sessions",
        "feedback",
        "monitoring",
        "alerts",
        "user_management",
        "knowledge_management",
        "system_config",
    },
}


def check_role_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())
