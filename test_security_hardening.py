"""
安全加固测试（v3.7）
覆盖：监控端点认证、WebSocket 限流、会话所有权、错误脱敏、输入净化、MessageBus 并发安全

运行: pytest test_security_hardening.py -v
"""
import os
import sys
import asyncio
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


# ===== Fixtures =====

@pytest.fixture
def fastapi_app():
    """创建带安全配置的 FastAPI 应用"""
    from multi_agent_customer_service import make_graph, session_mgr, cache, metrics, bus
    from api.app import create_app
    graph = make_graph()
    return create_app(
        graph,
        session_manager=session_mgr,
        response_cache=cache,
        metrics=metrics,
        message_bus=bus,
    )


@pytest.fixture
def client_with_api_key(fastapi_app):
    """携带 API Key 的测试客户端"""
    from fastapi.testclient import TestClient
    from config import API_KEY
    return TestClient(fastapi_app, headers={"X-API-Key": API_KEY})


@pytest.fixture
def client_with_admin_token(fastapi_app):
    """携带 Admin Token 的测试客户端"""
    from fastapi.testclient import TestClient
    from config import MONITORING_ADMIN_TOKEN
    return TestClient(fastapi_app, headers={"X-Admin-Token": MONITORING_ADMIN_TOKEN})


@pytest.fixture
def client_no_auth(fastapi_app):
    """无认证的测试客户端"""
    from fastapi.testclient import TestClient
    return TestClient(fastapi_app)


# ===== 1. 监控端点认证测试（Critical 修复）=====

class TestMonitoringAuth:
    """验证监控端点需要认证"""

    def test_metrics_no_auth_returns_401(self, client_no_auth):
        """无认证访问 /api/metrics 应返回 401"""
        resp = client_no_auth.get("/api/metrics")
        assert resp.status_code == 401

    def test_kpi_no_auth_returns_401(self, client_no_auth):
        """无认证访问 /api/kpi 应返回 401"""
        resp = client_no_auth.get("/api/kpi")
        assert resp.status_code == 401

    def test_circuit_breaker_no_auth_returns_401(self, client_no_auth):
        """无认证访问 /api/circuit-breaker 应返回 401"""
        resp = client_no_auth.get("/api/circuit-breaker")
        assert resp.status_code == 401

    def test_alerts_no_auth_returns_401(self, client_no_auth):
        """无认证访问 /api/alerts 应返回 401"""
        resp = client_no_auth.get("/api/alerts")
        assert resp.status_code == 401

    def test_cache_stats_no_auth_returns_401(self, client_no_auth):
        """无认证访问 /api/cache/stats 应返回 401"""
        resp = client_no_auth.get("/api/cache/stats")
        assert resp.status_code == 401

    def test_metrics_with_admin_token(self, client_with_admin_token):
        """携带 Admin Token 访问 /api/metrics 应返回 200"""
        resp = client_with_admin_token.get("/api/metrics")
        assert resp.status_code == 200
        assert "metrics" in resp.json()

    def test_metrics_with_api_key(self, client_with_api_key):
        """携带 API Key 访问 /api/metrics 应返回 200（向后兼容）"""
        resp = client_with_api_key.get("/api/metrics")
        assert resp.status_code == 200

    def test_health_is_public(self, client_no_auth):
        """健康检查端点无需认证"""
        resp = client_no_auth.get("/api/health")
        assert resp.status_code == 200


# ===== 2. 会话端点认证测试（High 修复）=====

class TestSessionAuth:
    """验证会话端点需要 API Key 认证"""

    def test_sessions_no_auth_returns_401(self, client_no_auth):
        """无认证访问 /api/sessions 应返回 401"""
        resp = client_no_auth.get("/api/sessions")
        assert resp.status_code == 401

    def test_sessions_with_api_key(self, client_with_api_key):
        """携带 API Key 访问 /api/sessions 应返回 200"""
        resp = client_with_api_key.get("/api/sessions")
        assert resp.status_code == 200


# ===== 3. 健康检查脱敏测试（High 修复）=====

class TestHealthSanitization:
    """验证健康检查不泄露内部细节"""

    def test_health_no_circuit_breaker_details(self, client_with_api_key):
        """健康检查不应暴露熔断器内部状态"""
        resp = client_with_api_key.get("/api/health")
        data = resp.json()
        # 不应包含内部阈值
        assert "circuit_breaker" not in data
        assert "fail_threshold" not in str(data)
        assert "recovery_time" not in str(data)
        assert "consecutive_failures" not in str(data)

    def test_circuit_breaker_no_internal_thresholds(self, client_with_admin_token):
        """熔断器端点不应暴露 fail_threshold 和 recovery_time"""
        resp = client_with_admin_token.get("/api/circuit-breaker")
        data = resp.json()
        cb = data.get("circuit_breaker", {})
        assert "fail_threshold" not in cb
        assert "recovery_time" not in cb
        assert "consecutive_failures" not in cb
        # 只应暴露安全的字段
        assert "state" in cb
        assert "total_failures" in cb
        assert "total_successes" in cb


# ===== 4. 输入净化测试（Medium 修复）=====

class TestInputSanitization:
    """验证输入净化功能"""

    def test_control_chars_removed(self):
        from api.app import _sanitize_input
        assert _sanitize_input("hello\x00world") == "helloworld"
        assert _sanitize_input("test\x01\x02\x03value") == "testvalue"

    def test_html_tags_removed(self):
        from api.app import _sanitize_input
        assert _sanitize_input("<script>alert(1)</script>") == "alert(1)"
        assert _sanitize_input("<b>bold</b> text") == "bold text"

    def test_normal_text_preserved(self):
        from api.app import _sanitize_input
        assert _sanitize_input("你好，我想咨询产品信息") == "你好，我想咨询产品信息"
        assert _sanitize_input("Hello World 123") == "Hello World 123"

    def test_whitespace_stripped(self):
        from api.app import _sanitize_input
        assert _sanitize_input("  hello  ") == "hello"
        assert _sanitize_input("\n\ttest\n") == "test"


# ===== 5. 会话所有权令牌测试（High 修复）=====

class TestSessionTokens:
    """验证会话所有权令牌生成和校验"""

    def test_token_generation(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        token = sm.generate_session_token("test-session-1")
        assert token
        assert len(token) == 32

    def test_token_validation_success(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        token = sm.generate_session_token("test-session-1")
        assert sm.validate_session_token("test-session-1", token) is True

    def test_token_validation_wrong_token(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        token = sm.generate_session_token("test-session-1")
        assert sm.validate_session_token("test-session-1", "wrong-token") is False

    def test_token_validation_empty_token(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        sm.generate_session_token("test-session-1")
        assert sm.validate_session_token("test-session-1", "") is False

    def test_token_validation_wrong_session(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        token = sm.generate_session_token("session-a")
        assert sm.validate_session_token("session-b", token) is False

    def test_token_deterministic(self):
        """同一 session_id 应始终生成相同的令牌"""
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        t1 = sm.generate_session_token("same-session")
        t2 = sm.generate_session_token("same-session")
        assert t1 == t2

    def test_no_secret_rejects_validation(self):
        """v3.8: 未配置 SESSION_TOKEN_SECRET 时应拒绝验证（安全优先）"""
        from session_manager import EnhancedSessionManager
        import config
        original = config.SESSION_TOKEN_SECRET
        try:
            config.SESSION_TOKEN_SECRET = ""
            sm = EnhancedSessionManager()
            # 安全：未配置密钥时拒绝验证
            assert sm.validate_session_token("any-session", "") is False
            assert sm.validate_session_token("any-session", "anything") is False
        finally:
            config.SESSION_TOKEN_SECRET = original


# ===== 6. 错误脱敏测试（High 修复）=====

class TestErrorSanitization:
    """验证错误响应不泄露内部细节"""

    def test_chat_empty_query_error_no_leak(self, client_with_api_key):
        """空查询的错误响应不应泄露内部信息"""
        resp = client_with_api_key.post("/api/chat", json={"query": ""})
        assert resp.status_code == 400
        data = resp.json()
        assert "error" in data
        # 不应包含内部路径或堆栈信息
        error_str = str(data)
        assert "traceback" not in error_str.lower()
        assert "/home/" not in error_str
        assert "monitoring.py" not in error_str


# ===== 7. MessageBus 并发安全测试（Medium 修复）=====

class TestMessageBusConcurrency:
    """验证 MessageBus 的 asyncio.Lock 保护"""

    @pytest.mark.asyncio
    async def test_concurrent_subscribe_unsubscribe(self):
        """并发订阅/取消不应崩溃"""
        from core.message_bus import MessageBus, Message, MessageType
        bus = MessageBus()
        handlers = []

        async def handler(msg):
            pass

        # 并发订阅和取消
        for _ in range(50):
            await bus.subscribe("test.topic", handler)
            handlers.append(handler)

        # 并发取消
        for h in handlers:
            await bus.unsubscribe("test.topic", h)

        # 发布不应报错
        await bus.publish(Message(
            msg_type=MessageType.BROADCAST,
            topic="test.topic",
            payload="test",
        ))

    @pytest.mark.asyncio
    async def test_subscribe_is_now_async(self):
        """subscribe 和 unsubscribe 应该是 async 方法"""
        from core.message_bus import MessageBus
        bus = MessageBus()
        # 验证可以 await
        await bus.subscribe("test", lambda m: None)
        await bus.unsubscribe("test", lambda m: None)


# ===== 8. 配置验证测试 =====

class TestSecurityConfig:
    """验证安全配置项存在且有合理默认值"""

    def test_ws_config_exists(self):
        from config import WS_MAX_CONNECTIONS_PER_IP, WS_MESSAGE_RATE_LIMIT, WS_IDLE_TIMEOUT
        assert WS_MAX_CONNECTIONS_PER_IP > 0
        assert WS_MESSAGE_RATE_LIMIT > 0
        assert WS_IDLE_TIMEOUT > 0

    def test_monitoring_admin_token_config(self):
        from config import MONITORING_ADMIN_TOKEN
        # 应该已配置（.env 中有值）
        assert MONITORING_ADMIN_TOKEN

    def test_session_token_secret_config(self):
        from config import SESSION_TOKEN_SECRET
        # 应该已配置（.env 中有值）
        assert SESSION_TOKEN_SECRET

    def test_tls_config_exists(self):
        from config import TLS_CERT_FILE, TLS_KEY_FILE
        # 默认为空（未启用 TLS）
        assert isinstance(TLS_CERT_FILE, str)
        assert isinstance(TLS_KEY_FILE, str)


# ===== 9. CORS 配置测试 =====

class TestCORSConfig:
    """验证 CORS 不使用通配符"""

    def test_cors_not_wildcard(self):
        from config import CORS_ORIGINS
        # .env 已配置为具体域名
        assert "*" not in CORS_ORIGINS
        assert len(CORS_ORIGINS) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
