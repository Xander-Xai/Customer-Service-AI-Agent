"""api/middleware.py 单元测试
覆盖：白名单跳过限流、安全响应头、限流逻辑、认证白名单、输入大小保护、CSRF 中间件

注意：FastAPI @app.middleware("http") 注册顺序与执行顺序相反。
最后注册的中间件最先执行（外层），最先注册的最后执行（内层）。
注册顺序：rate_limit → security_headers → auth → csrf → trace
执行顺序：trace → csrf → auth → security_headers → rate_limit
因此 rate_limit 是最内层，auth 在 rate_limit 之前运行。
测试限流时需要 DEV_MODE=True 或提供有效凭证。
"""

import os
import sys
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


# ── 测试用 App 工厂 ──


def _make_app():
    """创建带完整中间件栈的测试 FastAPI 应用"""
    from api.middleware import setup_middleware
    from fastapi.responses import HTMLResponse

    app = FastAPI()
    setup_middleware(app)

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return HTMLResponse("<html><body>welcome</body></html>")

    @app.get("/login.html", response_class=HTMLResponse)
    async def login_html():
        return HTMLResponse("<html><body>login</body></html>")

    @app.get("/admin.html", response_class=HTMLResponse)
    async def admin_html():
        return HTMLResponse("<html><body>admin</body></html>")

    @app.get("/widget.html", response_class=HTMLResponse)
    async def widget_html():
        return HTMLResponse("<html><body>widget</body></html>")

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/test")
    async def test_endpoint():
        return {"data": "test"}

    @app.post("/api/test")
    async def test_post():
        return {"data": "posted"}

    @app.post("/api/auth/login")
    async def login():
        return {"token": "abc"}

    @app.post("/api/auth/register")
    async def register():
        return {"id": 1}

    @app.get("/static/style.css")
    async def static_file():
        return "body{}"

    @app.get("/ws/connect")
    async def ws_endpoint():
        return {"ws": True}

    return app


@pytest.fixture()
def client():
    """DEV_MODE=False, 无 Redis: 用于测试安全头、认证、CSRF 等"""
    with (
        patch("api.middleware.get_redis_client", return_value=None),
        patch("api.middleware.DEV_MODE", False),
    ):
        app = _make_app()
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def dev_client():
    """DEV_MODE=True, 无 Redis: 用于测试限流（auth bypass）"""
    with (
        patch("api.middleware.get_redis_client", return_value=None),
        patch("api.middleware.DEV_MODE", True),
    ):
        app = _make_app()
        yield TestClient(app, raise_server_exceptions=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 白名单路径跳过限流
# ═══════════════════════════════════════════════════════════════════════════════


class TestRateLimitWhitelist:
    """白名单路径不被限流，即使大量请求也不会返回 429"""

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/api/health",
            "/login.html",
            "/admin.html",
            "/widget.html",
            "/static/style.css",
            "/ws/connect",
        ],
    )
    def test_whitelist_path_not_rate_limited(self, dev_client, path):
        """白名单路径即使超量请求也不应返回 429"""
        for _ in range(100):
            resp = dev_client.get(path)
        assert resp.status_code != 429

    def test_non_whitelist_path_gets_rate_limited(self, dev_client):
        """非白名单路径在超量请求后应被限流（返回 429）"""
        for _ in range(60):
            dev_client.get("/api/test")
        resp = dev_client.get("/api/test")
        assert resp.status_code == 429
        assert "Rate limit" in resp.json()["error"]

    def test_case_insensitive_health_whitelist(self, dev_client):
        """v5.3: 大小写混合的 /API/Health 也应被白名单跳过限流"""
        for _ in range(100):
            resp = dev_client.get("/API/Health")
        assert resp.status_code != 429


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 安全响应头
# ═══════════════════════════════════════════════════════════════════════════════


class TestSecurityHeaders:
    """验证所有安全响应头正确注入"""

    def test_security_headers_present(self, client):
        """白名单路径也能拿到安全响应头"""
        resp = client.get("/api/health")
        headers = resp.headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        # CSP 仅对 HTML 响应设置（静态资源/API 无需 CSP，避免 Lighthouse 误报）
        assert headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
        assert headers["Permissions-Policy"] == "camera=(), geolocation=()"
        assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_csp_on_html_response(self, client):
        """HTML 响应包含 CSP header 和 nonce"""
        resp = client.get("/")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "script-src" in csp
        assert "frame-ancestors" in csp
        assert "'nonce-" in csp

    def test_no_csp_on_api_response(self, client):
        """API 响应不应包含 CSP header（避免 Lighthouse 误报 unneeded header）"""
        resp = client.get("/api/health")
        assert "Content-Security-Policy" not in resp.headers

    def test_no_xss_protection_header(self, client):
        """不再设置 X-XSS-Protection header（已被浏览器弃用，Lighthouse 标记为 unneeded）"""
        resp = client.get("/api/health")
        assert "X-XSS-Protection" not in resp.headers

    def test_trace_id_present(self, client):
        """每个响应包含 X-Trace-ID"""
        resp = client.get("/api/health")
        assert "X-Trace-ID" in resp.headers
        assert len(resp.headers["X-Trace-ID"]) > 0

    def test_trace_id_unique_per_request(self, client):
        """不同请求的 Trace ID 不同"""
        r1 = client.get("/api/health")
        r2 = client.get("/api/health")
        assert r1.headers["X-Trace-ID"] != r2.headers["X-Trace-ID"]


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 限流逻辑
# ═══════════════════════════════════════════════════════════════════════════════


class TestRateLimitLogic:
    """测试内存限流：阈值内放行，超过阈值 429。
    使用 DEV_MODE=True 绕过认证，使请求能到达限流中间件。
    """

    def test_under_limit_returns_200(self, dev_client):
        """请求数未超限，返回 200"""
        resp = dev_client.get("/api/test")
        assert resp.status_code == 200

    def test_at_limit_returns_429(self, dev_client):
        """请求到达 RATE_LIMIT_MAX(60) 后返回 429"""
        for i in range(60):
            r = dev_client.get("/api/test")
            assert r.status_code == 200, f"第 {i + 1} 次请求应成功"
        resp = dev_client.get("/api/test")
        assert resp.status_code == 429
        assert "Rate limit" in resp.json()["error"]

    def test_custom_rate_limit_max(self):
        """通过 RATE_LIMIT_MAX 环境变量自定义限流阈值"""
        with (
            patch.dict(os.environ, {"RATE_LIMIT_MAX": "5", "RATE_LIMIT_WINDOW": "60"}),
            patch("api.middleware.get_redis_client", return_value=None),
            patch("api.middleware.DEV_MODE", True),
        ):
            app = _make_app()
            c = TestClient(app, raise_server_exceptions=False)
            for _ in range(5):
                c.get("/api/test")
            resp = c.get("/api/test")
            assert resp.status_code == 429

    def test_login_rate_limit(self, dev_client):
        """登录接口独立限流：5 次/5 分钟"""
        for _ in range(5):
            dev_client.post("/api/auth/login")
        resp = dev_client.post("/api/auth/login")
        assert resp.status_code == 429
        assert "登录" in resp.json()["error"]

    def test_register_rate_limit(self, dev_client):
        """注册接口独立限流：3 次/小时"""
        for _ in range(3):
            dev_client.post("/api/auth/register")
        resp = dev_client.post("/api/auth/register")
        assert resp.status_code == 429
        assert "注册" in resp.json()["error"]

    def test_rate_limit_window_expiry(self, dev_client):
        """限流窗口过期后重置（模拟时间推进）"""
        # 填满限流桶
        for _ in range(60):
            dev_client.get("/api/test")
        assert dev_client.get("/api/test").status_code == 429

        # 推进时间超过窗口
        with patch("time.time", return_value=time.time() + 61):
            resp = dev_client.get("/api/test")
            assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 认证白名单
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuthWhitelist:
    """认证白名单路径无需凭证即可访问"""

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/api/health",
            "/login.html",
            "/admin.html",
            "/widget.html",
            "/static/style.css",
        ],
    )
    def test_whitelist_no_auth_required(self, client, path):
        """白名单路径在无认证时返回 200"""
        resp = client.get(path)
        assert resp.status_code == 200

    def test_login_endpoint_no_auth(self, client):
        """登录端点不需要认证，但 CSRF 仍生效（POST 需要 token）"""
        client.cookies.set("dummy", "cookie")
        resp = client.post("/api/auth/login")
        # 认证中间件放行，但 CSRF 中间件要求 POST 有 token → 403
        assert resp.status_code == 403

    def test_register_endpoint_no_auth(self, client):
        """注册端点不需要认证，但 CSRF 仍生效"""
        client.cookies.set("dummy", "cookie")
        resp = client.post("/api/auth/register")
        assert resp.status_code == 403

    def test_protected_endpoint_requires_auth(self, client):
        """非白名单 GET /api/test 需要认证（DEV_MODE=False 时返回 401）"""
        resp = client.get("/api/test")
        assert resp.status_code == 401
        assert "Unauthorized" in resp.json()["error"]

    def test_case_insensitive_health_auth_bypass(self, client):
        """v5.3: 大小写混合路径不被 auth 中间件拦截（不返回 401）"""
        resp = client.get("/API/Health")
        # 中间件正确放行（不返回 401），路由可能 404（大小写不匹配）
        assert resp.status_code != 401


# ═══════════════════════════════════════════════════════════════════════════════
# 5. 输入大小保护
# ═══════════════════════════════════════════════════════════════════════════════


class TestInputSizeProtection:
    """超过 1MB 的 POST 请求应返回 413"""

    def test_oversized_post_returns_413(self, dev_client):
        """POST /api/test 带超大 body 返回 413"""
        # 发送实际超过 5MB 的请求体，触发中间件 Payload too large 检查
        large_body = b"x" * 6_000_000  # 6MB
        resp = dev_client.post("/api/test", content=large_body)
        assert resp.status_code == 413
        assert "Payload too large" in resp.json()["error"]

    def test_normal_sized_post_allowed(self, dev_client):
        """正常大小的 POST 请求不受影响"""
        resp = dev_client.post("/api/test", json={"message": "hello"})
        assert resp.status_code != 413

    def test_size_check_only_for_post(self, dev_client):
        """GET 请求不受 Content-Length 限制"""
        headers = {"Content-Length": "2000000"}
        resp = dev_client.get("/api/health", headers=headers)
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CSRF 中间件
# ═══════════════════════════════════════════════════════════════════════════════


class TestCSRFLayer:
    """CSRF 双提交 Cookie 验证"""

    def test_dev_mode_skips_csrf(self, dev_client):
        """DEV_MODE=True 时跳过 CSRF 检查，POST 正常返回"""
        resp = dev_client.post("/api/test", json={"a": 1})
        assert resp.status_code == 200

    def test_get_requests_skip_csrf(self, client):
        """GET 请求不受 CSRF 检查"""
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_csrf_cookie_set_on_get(self, client):
        """GET 请求后响应包含 csrf_token cookie"""
        resp = client.get("/api/health")
        assert "csrf_token" in resp.cookies

    def test_bearer_token_skips_csrf(self, client):
        """携带 Bearer Token 的请求跳过 CSRF（可能被 auth 拦截但不是 CSRF 错误）"""
        headers = {"Authorization": "Bearer some-token"}
        resp = client.post("/api/test", json={}, headers=headers)
        assert resp.status_code != 403

    def test_api_key_skips_csrf(self, client):
        """携带 X-API-Key 的请求跳过 CSRF"""
        headers = {"X-API-Key": "some-key"}
        resp = client.post("/api/test", json={}, headers=headers)
        assert resp.status_code != 403

    def test_admin_token_skips_csrf(self, client):
        """携带 X-Admin-Token 的请求跳过 CSRF"""
        headers = {"X-Admin-Token": "some-token"}
        resp = client.post("/api/test", json={}, headers=headers)
        assert resp.status_code != 403

    def test_bearer_with_cookies_skips_csrf(self, client):
        """v5.3: 同时携带 Bearer Token 和 Cookie 时跳过 CSRF（API 调用非浏览器发起）"""
        headers = {"Authorization": "Bearer some-token"}
        cookies = {"dummy": "cookie"}
        resp = client.post("/api/test", json={}, headers=headers, cookies=cookies)
        # Bearer Token 触发显式跳过 CSRF，不会返回 403
        assert resp.status_code != 403

    def test_csrf_missing_tokens_returns_403(self, client):
        """浏览器 POST 无 CSRF token 且无其他凭证返回 403 或 401"""
        resp = client.post("/api/test", json={"a": 1})
        # auth 在 CSRF 之前执行，可能先返回 401
        assert resp.status_code in (401, 403)

    def test_csrf_matching_tokens_passes(self, client):
        """CSRF Cookie 和 Header 匹配时通过验证"""
        # 先通过 GET 获取 csrf_token cookie
        get_resp = client.get("/api/health")
        csrf_token = get_resp.cookies.get("csrf_token")
        assert csrf_token is not None

        # 带匹配的 cookie + header 发 POST
        resp = client.post(
            "/api/test",
            json={"a": 1},
            headers={"X-CSRF-Token": csrf_token},
            cookies={"csrf_token": csrf_token},
        )
        # CSRF 通过（不是 403），可能被 auth 拦截返回 401
        assert resp.status_code != 403


# ═══════════════════════════════════════════════════════════════════════════════
# 7. 角色权限检查
# ═══════════════════════════════════════════════════════════════════════════════


class TestRolePermissions:
    """check_role_permission 函数测试"""

    def test_customer_permissions(self):
        from api.middleware import check_role_permission

        assert check_role_permission("customer", "chat") is True
        assert check_role_permission("customer", "user_management") is False

    def test_admin_permissions(self):
        from api.middleware import check_role_permission

        assert check_role_permission("admin", "chat") is True
        assert check_role_permission("admin", "user_management") is True
        assert check_role_permission("admin", "knowledge_management") is True
        assert check_role_permission("admin", "system_config") is True

    def test_unknown_role_has_no_permissions(self):
        from api.middleware import check_role_permission

        assert check_role_permission("hacker", "chat") is False
        assert check_role_permission("", "chat") is False
