"""
v4.0 测试：用户认证 + 数据库 + 知识库管理 + 告警通知
"""
import os
import sys
import time
import json
import pytest

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 设置测试环境变量（避免 config.py 校验失败）
os.environ.setdefault("API_KEY_ENABLED", "false")
os.environ.setdefault("API_KEY", "test-key-for-tests-only")
os.environ.setdefault("SESSION_TOKEN_SECRET", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")  # P0-2: 固定管理员密码便于测试


# ===== 数据库模型测试 =====

class TestDatabaseModels:
    """数据库模型基础测试"""

    def test_import_models(self):
        from db.models import Base, User, ChatHistory, AuditLog
        assert hasattr(User, '__tablename__')
        assert hasattr(ChatHistory, '__tablename__')
        assert hasattr(AuditLog, '__tablename__')

    def test_database_init(self):
        from db.database import init_db, engine
        init_db()
        # 验证表已创建
        from db.models import Base
        tables = Base.metadata.tables.keys()
        assert "users" in tables
        assert "chat_histories" in tables
        assert "audit_logs" in tables


# ===== 认证服务测试 =====

class TestAuthService:
    """认证业务逻辑测试"""

    def test_hash_password(self):
        from auth.service import hash_password, verify_password
        pwd = "test123456"
        hashed = hash_password(pwd)
        assert "$" in hashed
        assert verify_password(pwd, hashed)
        assert not verify_password("wrong", hashed)

    def test_hash_password_different_salt(self):
        from auth.service import hash_password
        h1 = hash_password("same_password")
        h2 = hash_password("same_password")
        # 不同 salt → 不同 hash
        assert h1 != h2

    def test_create_and_decode_token(self):
        from auth.service import create_token, decode_token
        token = create_token(1, "testuser", "customer")
        payload = decode_token(token)
        assert isinstance(payload, dict)
        assert payload["sub"] == 1
        assert payload["username"] == "testuser"
        assert payload["role"] == "customer"
        assert "exp" in payload
        assert "iat" in payload

    def test_decode_invalid_token(self):
        from auth.service import decode_token
        assert decode_token("invalid.token.here") is None
        assert decode_token("") is None
        assert decode_token("only.two") is None

    def test_register_and_authenticate_user(self):
        from auth.service import register_user, authenticate_user
        import time
        unique = f"tuser_{int(time.time()*1000)}"
        # 注册
        result = register_user(unique, "password123", "测试用户")
        assert result["success"] is True
        assert "user_id" in result

        # 重复注册
        result2 = register_user(unique, "password123")
        assert result2["success"] is False
        assert "已存在" in result2["error"]

        # 认证
        auth = authenticate_user(unique, "password123")
        assert isinstance(auth, dict)
        assert "token" in auth
        assert auth["username"] == unique
        assert auth["role"] == "customer"

        # 错误密码
        auth2 = authenticate_user(unique, "wrong_password")
        assert auth2 is None

        # 不存在的用户
        auth3 = authenticate_user("nonexistent_xxxx", "password123")
        assert auth3 is None

    def test_init_default_admin(self):
        from auth.service import init_default_admin, authenticate_user
        init_default_admin()
        auth = authenticate_user("admin", "admin123")
        assert isinstance(auth, dict)
        assert auth["role"] == "admin"
        assert auth["username"] == "admin"


# ===== 告警通知测试 =====

class TestAlertNotifier:
    """告警通知模块测试"""

    def test_notifier_init(self):
        from alerts.notifier import AlertNotifier
        notifier = AlertNotifier()
        config = notifier.get_config()
        assert "webhooks" in config
        assert "email_enabled" in config

    def test_alert_history(self):
        from alerts.notifier import AlertNotifier
        notifier = AlertNotifier()
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(
            notifier.send_alert("test", "test content", "info")
        )
        history = notifier.get_history()
        assert len(history) >= 1
        assert history[-1]["title"] == "test"

    def test_notifier_config_display(self):
        from alerts.notifier import AlertNotifier
        notifier = AlertNotifier()
        config = notifier.get_config()
        # 不应暴露完整 URL
        for wh in config["webhooks"]:
            if wh.get("url"):
                assert "..." in wh["url"]


# ===== 知识库管理测试 =====

class TestKnowledgeRouter:
    """知识库管理路由测试（仅验证模块可导入）"""

    def test_import_router(self):
        from knowledge.router import router
        assert hasattr(router, 'routes')

    def test_import_init(self):
        from knowledge import knowledge_router
        assert hasattr(knowledge_router, 'routes')


# ===== 认证路由测试 =====

class TestAuthRouter:
    """认证路由测试（仅验证模块可导入）"""

    def test_import_router(self):
        from auth.router import router
        assert hasattr(router, 'routes')

    def test_import_init(self):
        from auth import auth_router
        assert hasattr(auth_router, 'routes')


# ===== API 集成测试（FastAPI TestClient） =====

class TestAPIIntegration:
    """v4.0 API 集成测试"""

    def _get_client(self):
        """创建测试客户端"""
        from fastapi.testclient import TestClient
        from db.database import init_db
        from auth.service import init_default_admin
        init_db()
        init_default_admin()

        from multi_agent_customer_service import build_graph
        from core.container import ServiceContainer
        from api.app import create_app
        from auth.router import router as auth_router
        from knowledge.router import router as knowledge_router
        from alerts.router import router as alerts_router

        container = ServiceContainer()
        graph_app = build_graph(container)
        app = create_app(
            graph_app,
            session_manager=container.session_mgr,
            response_cache=container.cache,
            metrics=container.metrics,
            message_bus=container.bus,
            sla_alert_mgr=container.sla_alert_mgr,
        )
        app.include_router(auth_router)
        app.include_router(knowledge_router)
        app.include_router(alerts_router)
        return TestClient(app)

    def test_login_success(self):
        client = self._get_client()
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["role"] == "admin"

    def test_login_wrong_password(self):
        client = self._get_client()
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401

    def test_register_success(self):
        client = self._get_client()
        import time
        unique = f"newuser_{int(time.time()*1000)}"
        resp = client.post("/api/auth/register", json={
            "username": unique,
            "password": "password123",
            "display_name": "新用户",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == unique

    def test_register_duplicate(self):
        client = self._get_client()
        import time
        unique = f"dup_{int(time.time()*1000)}"
        # 注册两次
        client.post("/api/auth/register", json={"username": unique, "password": "pass123"})
        resp = client.post("/api/auth/register", json={"username": unique, "password": "pass123"})
        assert resp.status_code == 400

    def test_me_with_token(self):
        client = self._get_client()
        # 登录获取 token
        login_resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        token = login_resp.json()["token"]
        # 获取用户信息
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["username"] == "admin"

    def test_me_without_token(self):
        client = self._get_client()
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_users_list_requires_admin(self):
        client = self._get_client()
        import time
        unique = f"normal_{int(time.time()*1000)}"
        # 普通用户
        client.post("/api/auth/register", json={"username": unique, "password": "pass123"})
        login_resp = client.post("/api/auth/login", json={"username": unique, "password": "pass123"})
        token = login_resp.json()["token"]
        resp = client.get("/api/auth/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403  # 非管理员

    def test_users_list_admin(self):
        client = self._get_client()
        login_resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        token = login_resp.json()["token"]
        resp = client.get("/api/auth/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert "users" in resp.json()

    def test_health_endpoint(self):
        client = self._get_client()
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data

    def test_knowledge_stats(self):
        client = self._get_client()
        # 需要认证
        login_resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        token = login_resp.json()["token"]
        resp = client.get("/api/knowledge/stats", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert "available" in data

    def test_auth_register_validates_username(self):
        client = self._get_client()
        # 用户名太短
        resp = client.post("/api/auth/register", json={"username": "ab", "password": "pass123"})
        assert resp.status_code == 422  # Pydantic 验证失败

    def test_auth_register_validates_password(self):
        client = self._get_client()
        # 密码太短
        resp = client.post("/api/auth/register", json={"username": "validuser", "password": "123"})
        assert resp.status_code == 422
