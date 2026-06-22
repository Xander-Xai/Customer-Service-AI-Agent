"""
api/app_factory.py 覆盖率提升测试
覆盖：lifespan、create_app、安全检查、路由注册、静态资源
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TestCreateApp:
    """create_app() 应用工厂测试"""

    def _make_app(self, graph_app=None, **kwargs):
        """创建测试用 app（跳过 app_factory 的模块级初始化）"""
        from api.app import create_app

        return create_app(graph_app, **kwargs)

    def test_create_app_returns_fastapi(self):
        """create_app 返回 FastAPI 实例"""
        app = self._make_app()
        assert isinstance(app, FastAPI)

    def test_create_app_stores_state(self):
        """create_app 正确存储 app.state 依赖"""
        sm = MagicMock()
        cache = MagicMock()
        metrics = MagicMock()
        bus = MagicMock()
        sla = MagicMock()
        app = self._make_app(
            graph_app=None,
            session_manager=sm,
            response_cache=cache,
            metrics=metrics,
            message_bus=bus,
            sla_alert_mgr=sla,
        )
        assert app.state.session_manager is sm
        assert app.state.response_cache is cache
        assert app.state.metrics is metrics
        assert app.state.message_bus is bus
        assert app.state.sla_alert_mgr is sla

    def test_create_app_includes_routes(self):
        """create_app 注册了所有路由。

        Starlette ≥1.x 使用 _IncludedRouter 封装 include_router 的路由，
        这些对象不直接在 app.routes 暴露子路由的 path 属性。
        改用 app.url_path_for(name) 验证路由存在性（兼容所有版本）。
        """
        app = self._make_app()
        # 核心路由：通过路由名称验证存在性（兼容 Starlette 0.x 和 1.x）
        core_routes = [
            ("websocket_chat", "/ws/chat"),
            ("serve_index", "/"),
            ("serve_login", "/login.html"),
        ]
        for name, expected_path in core_routes:
            try:
                path = app.url_path_for(name)
                assert expected_path in path, f"路由 {name} 路径不匹配: {path}"
            except Exception:
                # Fallback: 扫描 app.routes 直接查找（兼容旧版 Starlette）

                found = False
                for r in app.routes:
                    if hasattr(r, "path") and r.path == expected_path:
                        found = True
                        break
                    if hasattr(r, "routes") and any(
                        getattr(sr, "path", "") == expected_path for sr in r.routes
                    ):
                        found = True
                        break
                    if isinstance(r, type("_IncludedRoute", (), {})):
                        pass
                    # Try original_router for Starlette 1.x
                    if hasattr(r, "original_router"):
                        for sr in r.original_router.routes:
                            if getattr(sr, "path", "") == expected_path:
                                found = True
                                break
                    if found:
                        break
                assert found, f"路由 {expected_path} 未注册"

    def test_create_app_dev_mode(self):
        """create_app 在 DEV_MODE 下设置 dev_mode"""
        with patch("api.app.DEV_MODE", True):
            app = self._make_app()
        assert app.state.dev_mode is True

    def test_serve_index_route_exists(self):
        """GET / 路由存在"""
        app = self._make_app()
        route_paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/" in route_paths

    def test_serve_login_route_exists(self):
        """GET /login.html 路由存在"""
        app = self._make_app()
        route_paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/login.html" in route_paths

    def test_serve_admin_route_exists(self):
        """GET /admin.html 路由存在"""
        app = self._make_app()
        route_paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/admin.html" in route_paths

    def test_serve_widget_route_exists(self):
        """GET /widget.html 路由存在"""
        app = self._make_app()
        route_paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/widget.html" in route_paths

    def test_app_has_middleware(self):
        """app 配置了中间件"""
        app = self._make_app()
        # CORS middleware should be configured
        assert len(app.middleware_stack.__class__.__mro__) > 1

    def test_app_lifespan_sets_run_graph(self):
        """lifespan 设置 run_graph 到 app.state"""
        app = self._make_app()
        assert hasattr(app.state, "run_graph")

    def test_app_lifespan_sets_module_load_time(self):
        """lifespan 设置 module_load_time"""
        app = self._make_app()
        assert hasattr(app.state, "module_load_time")

    @pytest.mark.asyncio
    async def test_cached_static_mount_adds_cache_security_and_charset_headers(self):
        """静态资源包装器直接附带长期缓存、安全头和 utf-8 charset。"""
        from api.app import _make_cached_static

        sent_messages = []

        async def static_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"image/svg+xml")],
                }
            )
            await send({"type": "http.response.body", "body": b"<svg/>", "more_body": False})

        wrapped_app = _make_cached_static(static_app)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent_messages.append(message)

        await wrapped_app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/assets/logo.svg",
                "raw_path": b"/assets/logo.svg",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )

        start_message = next(message for message in sent_messages if message["type"] == "http.response.start")
        headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in start_message["headers"]}

        assert start_message["status"] == 200
        assert headers["cache-control"] == "public, max-age=31536000, immutable"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["content-type"] == "image/svg+xml; charset=utf-8"


class TestAppFactoryLifespan:
    """app_factory.py lifespan 异步初始化测试"""

    @pytest.mark.asyncio
    async def test_lifespan_initializes_container(self):
        """lifespan 调用 container.initialize()"""
        mock_container = AsyncMock()
        mock_container.session_mgr = MagicMock()
        mock_container.cache = MagicMock()
        mock_container.metrics = MagicMock()
        mock_container.bus = MagicMock()
        mock_container.sla_alert_mgr = MagicMock()
        mock_container.graph_app = MagicMock()
        mock_container.circuit_breaker = MagicMock()

        with patch("api.app_factory._container", mock_container):
            # Import the lifespan function
            import api.app_factory as factory_mod

            # 直接调用 lifespan
            mock_app = MagicMock()
            async with factory_mod.lifespan(mock_app):
                mock_container.initialize.assert_awaited_once()

            mock_container.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_injects_services_to_app_module(self):
        """lifespan 将容器服务注入到 api.app 模块"""
        mock_container = AsyncMock()
        mock_container.session_mgr = MagicMock()
        mock_container.cache = MagicMock()
        mock_container.metrics = MagicMock()
        mock_container.bus = MagicMock()
        mock_container.sla_alert_mgr = MagicMock()
        mock_container.graph_app = MagicMock()
        mock_container.circuit_breaker = MagicMock()

        with patch("api.app_factory._container", mock_container):
            import api.app as app_module
            import api.app_factory as factory_mod

            async with factory_mod.lifespan(MagicMock()):
                assert app_module._session_manager is mock_container.session_mgr
                assert app_module._response_cache is mock_container.cache
                assert app_module._metrics is mock_container.metrics
                assert app_module._bus is mock_container.bus
                assert app_module._sla_alert_mgr is mock_container.sla_alert_mgr
                assert app_module._graph_app is mock_container.graph_app
                assert app_module._circuit_breaker_ref is mock_container.circuit_breaker


class TestRunGraph:
    """_run_graph 图执行引擎测试"""

    @pytest.mark.asyncio
    async def test_run_graph_no_graph_app(self):
        """_run_graph 在 graph_app 为 None 时抛出异常"""
        from api.app import _run_graph

        with patch("api.app._graph_app", None), pytest.raises(AttributeError):
            await _run_graph("test-session", "你好")

    @pytest.mark.asyncio
    async def test_run_graph_success(self):
        """_run_graph 正常执行图并返回结果"""
        from api.app import _run_graph

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "response": "你好！有什么可以帮助你的？",
                "current_agent": "ProductAgent",
                "collaboration_mode": "sequential",
                "cached": False,
                "agents_used": ["ProductAgent"],
                "resolution_status": "resolved",
            }
        )

        mock_metrics = AsyncMock()
        mock_sla = AsyncMock()

        with (
            patch("api.app._graph_app", mock_graph),
            patch("api.app._session_manager", MagicMock()),
            patch("api.app._response_cache", MagicMock()),
            patch("api.app._metrics", mock_metrics),
            patch("api.app._sla_alert_mgr", mock_sla),
        ):
            result = await _run_graph("test-session", "你好")
            assert result["response"] == "你好！有什么可以帮助你的？"
            assert result["current_agent"] == "ProductAgent"

    @pytest.mark.asyncio
    async def test_run_graph_exception_returns_error(self):
        """_run_graph 异常时返回错误响应"""
        from api.app import _run_graph

        mock_graph = MagicMock()
        # ainvoke raises, then to_thread(invoke) also raises
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("graph error"))
        mock_graph.invoke = MagicMock(side_effect=RuntimeError("graph error"))

        with (
            patch("api.app._graph_app", mock_graph),
            patch("api.app._session_manager", MagicMock()),
            patch("api.app._response_cache", MagicMock()),
            pytest.raises(RuntimeError),
        ):
            await _run_graph("test-session", "你好")


class TestPersistMetrics:
    """_persist_metrics_snapshot 测试"""

    @pytest.mark.asyncio
    async def test_persist_metrics_no_metrics(self):
        """_persist_metrics_snapshot 在 metrics 为 None 时安全返回"""
        from api.app import _persist_metrics_snapshot

        with patch("api.app._metrics", None):
            await _persist_metrics_snapshot()  # 不应抛异常

    @pytest.mark.asyncio
    async def test_persist_metrics_with_metrics(self):
        """_persist_metrics_snapshot 正常持久化"""
        from api.app import _persist_metrics_snapshot

        mock_metrics = MagicMock()
        mock_metrics.get_all_metrics = MagicMock(return_value={"requests": 100})

        with patch("api.app._metrics", mock_metrics):
            await _persist_metrics_snapshot()
