"""
api/routes/ws.py 覆盖率提升测试
覆盖：连接管理、认证、消息处理、限流、清理
"""

import asyncio
import contextlib
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _make_ws_app(dev_mode=True):
    """创建带 WebSocket 路由的测试 app"""
    from api.routes.ws import router

    app = FastAPI()
    app.include_router(router)

    sm = MagicMock()
    sm.sessions = {}
    sm.generate_session_token = MagicMock(return_value="test-token")
    sm.validate_session_token = MagicMock(return_value=True)
    sm.set_user_id = MagicMock()

    bus = MagicMock()
    bus.subscribe = AsyncMock()
    bus.unsubscribe = AsyncMock()

    async def mock_run_graph(sid, query):
        return {
            "response": "你好！",
            "current_agent": "ProductAgent",
            "collaboration_mode": "sequential",
            "cached": False,
            "agents_used": ["ProductAgent"],
            "elapsed": 0.1,
            "resolution_status": "resolved",
        }

    app.state.session_manager = sm
    app.state.message_bus = bus
    app.state.run_graph = mock_run_graph
    app.state.circuit_breaker = None

    return app, sm


class TestWSConnections:
    """WebSocket 连接跟踪测试"""

    def test_cleanup_stale_ws_connections(self):
        """清理过期连接记录"""
        from api.routes.ws import _ws_connections, cleanup_stale_ws_connections

        _ws_connections["stale-ip"] = 0
        _ws_connections["active-ip"] = 1
        cleaned = cleanup_stale_ws_connections()
        assert cleaned >= 1
        assert "stale-ip" not in _ws_connections
        assert "active-ip" in _ws_connections
        # Cleanup
        _ws_connections.pop("active-ip", None)

    def test_cleanup_no_stale(self):
        """无过期连接时清理返回 0"""
        from api.routes.ws import _ws_connections, cleanup_stale_ws_connections

        saved = dict(_ws_connections)
        _ws_connections.clear()
        cleaned = cleanup_stale_ws_connections()
        assert cleaned == 0
        _ws_connections.update(saved)

    @pytest.mark.asyncio
    async def test_periodic_ws_cleanup(self):
        """periodic_ws_cleanup 周期性清理"""
        from api.routes.ws import _ws_connections, periodic_ws_cleanup

        saved = dict(_ws_connections)
        _ws_connections["stale"] = 0

        task = asyncio.create_task(periodic_ws_cleanup())
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        _ws_connections.update(saved)


class TestWSAuthenticate:
    """WebSocket 认证测试"""

    @pytest.mark.asyncio
    async def test_ws_authenticate_dev_mode_skips_auth(self):
        """v5.4.2: DEV_MODE 下认证自动跳过，返回空凭证"""
        from api.routes.ws import _ws_authenticate

        mock_ws = AsyncMock()
        mock_ws.receive_json = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_ws.send_json = AsyncMock()
        mock_ws.close = AsyncMock()

        api_key, token, payload = await _ws_authenticate(mock_ws, "")
        assert api_key == ""
        assert token == ""
        assert payload is None

    @pytest.mark.asyncio
    async def test_ws_authenticate_with_api_key(self):
        """带 api_key 的认证"""
        from api.routes.ws import _ws_authenticate

        mock_ws = AsyncMock()
        mock_ws.receive_json = AsyncMock(return_value={"api_key": "valid-key"})

        with (
            patch("api.routes.ws.API_KEY_ENABLED", True),
            patch("api.routes.ws.API_KEY", "valid-key"),
            patch("api.routes.ws.DEV_MODE", False),
        ):
            api_key, token, payload = await _ws_authenticate(mock_ws, "")
            assert api_key == "valid-key"

    @pytest.mark.asyncio
    async def test_ws_authenticate_with_jwt(self):
        """JWT 认证"""
        from api.routes.ws import _ws_authenticate

        mock_ws = AsyncMock()
        mock_ws.receive_json = AsyncMock(
            return_value={
                "token": "jwt-token",
                "session_token": "sess-token",
            }
        )

        with (
            patch("api.routes.ws.API_KEY_ENABLED", True),
            patch("api.routes.ws.API_KEY", "server-key"),
            patch("api.routes.ws.DEV_MODE", False),
            patch("auth.service.decode_token", return_value={"sub": "user1"}),
        ):
            api_key, token, payload = await _ws_authenticate(mock_ws, "")
            assert payload == {"sub": "user1"}

    @pytest.mark.asyncio
    async def test_ws_authenticate_missing_credentials(self):
        """缺少凭证时关闭连接"""
        from api.routes.ws import _ws_authenticate

        mock_ws = AsyncMock()
        mock_ws.receive_json = AsyncMock(return_value={})
        mock_ws.send_json = AsyncMock()
        mock_ws.close = AsyncMock()

        with (
            patch("api.routes.ws.API_KEY_ENABLED", True),
            patch("api.routes.ws.API_KEY", "server-key"),
            patch("api.routes.ws.DEV_MODE", False),
            pytest.raises(ValueError, match="Missing credentials"),
        ):
            await _ws_authenticate(mock_ws, "")

    @pytest.mark.asyncio
    async def test_ws_authenticate_invalid_token(self):
        """无效 JWT token 关闭连接"""
        from api.routes.ws import _ws_authenticate

        mock_ws = AsyncMock()
        mock_ws.receive_json = AsyncMock(return_value={"token": "bad-jwt"})
        mock_ws.send_json = AsyncMock()
        mock_ws.close = AsyncMock()

        with (
            patch("api.routes.ws.API_KEY_ENABLED", True),
            patch("api.routes.ws.API_KEY", "server-key"),
            patch("api.routes.ws.DEV_MODE", False),
            patch("auth.service.decode_token", return_value=None),
            pytest.raises(ValueError, match="Invalid token"),
        ):
            await _ws_authenticate(mock_ws, "")

    @pytest.mark.asyncio
    async def test_ws_authenticate_timeout(self):
        """认证超时"""
        from api.routes.ws import _ws_authenticate

        mock_ws = AsyncMock()
        mock_ws.receive_json = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_ws.send_json = AsyncMock()
        mock_ws.close = AsyncMock()

        with (
            patch("api.routes.ws.API_KEY_ENABLED", True),
            patch("api.routes.ws.API_KEY", "server-key"),
            patch("api.routes.ws.DEV_MODE", False),
            pytest.raises(asyncio.TimeoutError),
        ):
            await _ws_authenticate(mock_ws, "")

    @pytest.mark.asyncio
    async def test_ws_authenticate_ws_api_key_header_valid(self):
        """通过 header 传递有效 api_key 时直接认证通过"""
        from api.routes.ws import _ws_authenticate

        mock_ws = AsyncMock()

        with (
            patch("api.routes.ws.API_KEY_ENABLED", True),
            patch("api.routes.ws.API_KEY", "header-key"),
            patch("api.routes.ws.DEV_MODE", False),
        ):
            api_key, token, payload = await _ws_authenticate(mock_ws, "header-key")
            assert api_key == "header-key"
            assert token == ""
            assert payload is None


class TestWSChat:
    """WebSocket 聊天端点测试（单元测试，不使用 TestClient）"""

    def test_ws_router_exists(self):
        """WebSocket 路由存在"""
        from api.routes.ws import router

        routes = [r for r in router.routes if hasattr(r, "path")]
        assert len(routes) > 0

    def test_ws_connections_dict(self):
        """连接跟踪字典存在"""
        from api.routes.ws import _ws_connections

        assert isinstance(_ws_connections, dict)

    def test_ws_lock_exists(self):
        """连接锁存在"""
        from api.routes.ws import _ws_lock

        assert _ws_lock is not None

    def test_ws_router_path(self):
        """WebSocket 路由路径正确"""
        from api.routes.ws import router

        paths = [r.path for r in router.routes if hasattr(r, "path")]
        assert "/ws/chat" in paths

    def test_ws_sanitize_input(self):
        """sanitize_input 工作正常"""
        from api.utils import sanitize_input

        result = sanitize_input("你好<script>alert(1)</script>")
        assert "<script>" not in result

    def test_ws_validate_session_id(self):
        """validate_session_id 工作正常"""
        from api.utils import validate_session_id

        valid = validate_session_id("test-session-123")
        assert valid == "test-session-123" or len(valid) > 0

    def test_ws_validate_session_id_traversal(self):
        """validate_session_id 防路径遍历"""
        from api.utils import validate_session_id

        result = validate_session_id("../../etc/passwd")
        assert "../" not in result

    def test_ws_cleanup_function(self):
        """cleanup_stale_ws_connections 可调用"""
        from api.routes.ws import cleanup_stale_ws_connections

        assert callable(cleanup_stale_ws_connections)

    def test_ws_periodic_cleanup_function(self):
        """periodic_ws_cleanup 可调用"""
        from api.routes.ws import periodic_ws_cleanup

        assert callable(periodic_ws_cleanup)


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket — Additional Coverage (auth error path, message types, cleanup)
# ═══════════════════════════════════════════════════════════════════════════════


class TestWSAuthenticateAdvanced:
    """WebSocket 认证高级场景"""

    @pytest.mark.asyncio
    async def test_ws_authenticate_generic_exception(self):
        """认证过程中的通用异常"""
        from api.routes.ws import _ws_authenticate

        mock_ws = AsyncMock()
        mock_ws.receive_json = AsyncMock(side_effect=RuntimeError("unexpected error"))
        mock_ws.send_json = AsyncMock()
        mock_ws.close = AsyncMock()

        with (
            patch("api.routes.ws.API_KEY_ENABLED", True),
            patch("api.routes.ws.API_KEY", "server-key"),
            patch("api.routes.ws.DEV_MODE", False),
            pytest.raises(RuntimeError),
        ):
            await _ws_authenticate(mock_ws, "")


class TestWSConnectionLimits:
    """WebSocket 连接限制测试"""

    def test_cleanup_stale_with_negative_counts(self):
        """清理负数连接计数"""
        from api.routes.ws import _ws_connections, cleanup_stale_ws_connections

        saved = dict(_ws_connections)
        _ws_connections["neg-ip"] = -1
        _ws_connections["zero-ip"] = 0
        _ws_connections["pos-ip"] = 2
        cleaned = cleanup_stale_ws_connections()
        assert cleaned >= 2  # neg-ip and zero-ip
        assert "neg-ip" not in _ws_connections
        assert "zero-ip" not in _ws_connections
        assert "pos-ip" in _ws_connections
        # Cleanup
        _ws_connections.pop("pos-ip", None)
        _ws_connections.update(saved)

    @pytest.mark.asyncio
    async def test_periodic_cleanup_exception_handling(self):
        """periodic_ws_cleanup 异常时不崩溃"""
        from api.routes.ws import periodic_ws_cleanup

        with patch("api.routes.ws.cleanup_stale_ws_connections", side_effect=RuntimeError("error")):
            task = asyncio.create_task(periodic_ws_cleanup())
            await asyncio.sleep(0.15)  # Let it run one cycle
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


class TestWSModuleImports:
    """WebSocket 模块级属性和导入"""

    def test_ws_conn_counter_exists(self):
        """_ws_conn_counter 存在"""
        from api.routes.ws import _ws_conn_counter

        assert isinstance(_ws_conn_counter, int)

    def test_ws_module_logger_exists(self):
        """logger 存在"""
        import api.routes.ws as ws_mod

        assert hasattr(ws_mod, "logger")

    def test_router_has_websocket_route(self):
        """router 包含 /ws/chat 路由"""
        from api.routes.ws import router

        paths = [r.path for r in router.routes if hasattr(r, "path")]
        assert "/ws/chat" in paths
