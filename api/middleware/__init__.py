"""
API 中间件栈：限流、安全头、认证、分布式追踪
从 api/app.py create_app() 提取。
"""

import asyncio
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.middleware.trace_middleware import TraceMiddleware
from api.utils import check_admin_token, check_api_key, check_jwt_auth, is_authenticated
from core.config import (
    DEV_MODE,
)
from core.logger import get_logger

logger = get_logger("api.middleware")

# ── Redis 限流客户端（懒初始化）──
_redis_client = None


def get_redis_client():
    """获取 Redis 客户端单例（懒初始化，失败返回 None）"""
    global _redis_client
    if _redis_client is None:
        try:
            import redis

            from core.config import REDIS_URL

            _redis_client = redis.Redis.from_url(
                REDIS_URL, decode_responses=True, socket_connect_timeout=3
            )
            _redis_client.ping()
        except Exception as e:
            logger.debug(f"[Redis] 初始化失败: {e}")
            _redis_client = None
    return _redis_client


def _redis_rate_limit(client_ip: str, max_requests: int, window_seconds: int) -> bool | None:
    """Redis 滑动窗口限流，返回 True 表示允许，False 表示拒绝，None 表示 Redis 不可用"""
    r = get_redis_client()
    if not r:
        return None
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
        # v6.3: 返回 None 表示 Redis 不可用，而非 False（拒绝）
        # 避免因 Redis 连接异常导致所有请求被 429 拒绝
        return None


def setup_middleware(app: FastAPI):
    """注册所有 HTTP 中间件到 FastAPI 应用"""

    # ── 限流中间件 ──
    _rate_limit_store: dict[str, deque] = defaultdict(deque)
    _RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "60"))
    _RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))

    _RATE_LIMIT_STORE_MAX = 100_000

    def _sync_cleanup(store: dict, expiry: int) -> int:
        """同步清理过期条目，返回清理数量。在线程池中运行避免阻塞事件循环。"""
        now = time.time()
        expired_keys = [
            key
            for key, timestamps in store.items()
            if not timestamps or now - timestamps[-1] > expiry
        ]
        for key in expired_keys:
            store.pop(key, None)
        return len(expired_keys)

    async def _periodic_cleanup():
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(600)  # 每 10 分钟清理一次
            expiry = max(_RATE_LIMIT_WINDOW * 2, 300)  # 过期阈值 ≥ 300s
            cleaned = await loop.run_in_executor(None, _sync_cleanup, _rate_limit_store, expiry)
            if cleaned:
                logger.debug(
                    f"[RateLimit] 清理 {cleaned} 个过期条目，剩余 {len(_rate_limit_store)}"
                )

    # v5.4: 已迁移到 lifespan，移除废弃的 on_event
    def start_cleanup_task():
        """启动时注册周期性清理任务"""
        logger.info("[Middleware] 启动周期性清理任务")
        return asyncio.create_task(_periodic_cleanup())

    app.state.start_rate_limit_cleanup = start_cleanup_task

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        path_norm = request.url.path.rstrip("/").lower()
        if (
            path_norm
            in (
                "",
                "/api/health",
                "/login.html",
                "/admin.html",
                "/widget.html",
                "/theme-comparison.html",
            )
            or request.url.path.startswith("/static/")
            or request.url.path.startswith("/ws/")
        ):
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        auth_path = request.url.path
        if auth_path == "/api/auth/login":
            auth_key = f"auth_login:{client_ip}"
            q = _rate_limit_store[auth_key]
            while q and now - q[0] > 300:
                q.popleft()
            if len(q) >= 5:
                return JSONResponse({"error": "登录尝试过于频繁，请 5 分钟后重试"}, status_code=429)
            q.append(now)
            return await call_next(request)
        if auth_path == "/api/auth/register":
            auth_key = f"auth_register:{client_ip}"
            q = _rate_limit_store[auth_key]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= 3:
                return JSONResponse({"error": "注册过于频繁，请稍后再试"}, status_code=429)
            q.append(now)
            return await call_next(request)

        # Redis 优先，失败则回退到内存限流
        redis_result = _redis_rate_limit(client_ip, _RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW)
        if redis_result is True:
            return await call_next(request)
        elif redis_result is False:
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        # redis_result is None: Redis unavailable, fall through to in-memory

        # v5.3: 防止内存无限增长 — 新 IP 且存储超限时直接放行
        if client_ip not in _rate_limit_store and len(_rate_limit_store) >= _RATE_LIMIT_STORE_MAX:
            logger.warning(f"[RateLimit] 存储已达上限 {_RATE_LIMIT_STORE_MAX}，放行 {client_ip}")
            return await call_next(request)

        q = _rate_limit_store[client_ip]
        while q and now - q[0] > _RATE_LIMIT_WINDOW:
            q.popleft()
        if len(q) >= _RATE_LIMIT_MAX:
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        q.append(now)
        return await call_next(request)

    # ── 缓存策略中间件 ──
    # 静态资源（/assets/、/static/、/styles/）: 长缓存 + immutable
    # API 响应: no-store（防止缓存敏感数据）
    # HTML 页面: no-cache（每次验证）
    @app.middleware("http")
    async def cache_control_middleware(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path

        # Vite 构建产物（含 hash 文件名）→ 长缓存
        if (
            path.startswith("/assets/")
            or path.startswith("/static/")
            or path.startswith("/styles/")
        ):
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        # HTML 页面 → 每次验证
        elif path.endswith(".html") or path == "/":
            response.headers["cache-control"] = "no-cache"
            response.headers["Cache-Control"] = "no-cache"
        # API 响应 → 不缓存
        elif path.startswith("/api/"):
            response.headers["cache-control"] = "no-store"
            response.headers["Cache-Control"] = "no-store"

        return response

    # ── API Key / JWT 认证中间件 ──
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # 输入大小保护：拒绝超过 5MB 的 POST 请求体（与 MAX_IMAGE_SIZE_MB 一致）
        if request.method == "POST" and path.startswith("/api/"):
            cl = request.headers.get("content-length")
            if cl and int(cl) > 5000000:
                return JSONResponse({"error": "Payload too large"}, status_code=413)

        path_norm = path.rstrip("/").lower()
        if path_norm in (
            "",
            "/api/health",
            "/login.html",
            "/admin.html",
            "/widget.html",
            "/theme-comparison.html",
        ) or path.startswith("/static/"):
            return await call_next(request)
        if (
            path.startswith("/api/auth/login")
            or path.startswith("/api/auth/register")
            or path.startswith("/api/auth/refresh")
        ):
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
            or path.startswith("/api/admin/prompts")
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
    _CSRF_SKIP_PATHS = {
        "/",
        "/api/health",
        "/login.html",
        "/admin.html",
        "/widget.html",
        "/theme-comparison.html",
    }
    _CSRF_SKIP_PREFIXES = ("/static/", "/ws/")

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()

        # DEV_MODE 跳过 CSRF（与 auth_middleware 的 DEV_MODE bypass 一致）
        if DEV_MODE:
            return await call_next(request)

        # 安全方法、静态资源、WebSocket 跳过 CSRF
        path_lower = path.rstrip("/").lower()
        if method in _CSRF_SAFE_METHODS or path_lower in _CSRF_SKIP_PATHS:
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

        if any(path_lower.startswith(p) for p in _CSRF_SKIP_PREFIXES):
            return await call_next(request)

        # 无 Cookie 的请求不存在 CSRF 风险，跳过
        if not request.cookies:
            return await call_next(request)

        # 携带 API Key 或 Admin Token 的系统间调用跳过
        if request.headers.get("X-API-Key") or request.headers.get("X-Admin-Token"):
            return await call_next(request)

        # v5.3: Bearer Token 是 API 认证，非浏览器发起，跳过 CSRF
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
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
    app.add_middleware(TraceMiddleware)

    # ── 安全响应头 ──
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # v6.3: X-Frame-Options only for non-widget pages (widget needs iframe embedding)
        is_widget = request.url.path.rstrip("/").lower() == "/widget.html"
        if not is_widget:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # 仅对 HTML 页面设置 CSP（静态资源无需 CSP，避免 Lighthouse 误报）
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            # v6.3: widget.html 允许 iframe 嵌入，其他页面保持 frame-ancestors 'none'
            is_widget = request.url.path.rstrip("/").lower() == "/widget.html"
            frame_ancestors = "'self'" if is_widget else "'none'"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                "style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "img-src 'self' data: blob:; "
                f"frame-ancestors {frame_ancestors}"
            )

        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=()"

        # 确保 Content-Type 含 charset=utf-8 且为小写
        if content_type:
            import re

            if "charset=" in content_type.lower():
                new_ct = re.sub(r"charset=\S+", "charset=utf-8", content_type, flags=re.IGNORECASE)
                response.headers["content-type"] = new_ct
                response.headers["Content-Type"] = new_ct
            else:
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type.startswith("text/") or media_type in {
                    "application/javascript",
                    "application/x-javascript",
                    "application/json",
                    "application/manifest+json",
                    "application/xml",
                    "image/svg+xml",
                }:
                    new_ct = f"{media_type}; charset=utf-8"
                    response.headers["content-type"] = new_ct
                    response.headers["Content-Type"] = new_ct

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
