"""P0-04 SSE Identity Context Loss — identity propagation tests.

These tests verify that authenticated user identity is preserved across all
transport paths (REST / SSE / WebSocket / Multimodal) when reaching run_graph
and the graph state.

The defect: SSE and multimodal helpers create run_graph tasks without passing
user_id, while REST and WebSocket do. This means authenticated users silently
become anonymous on SSE paths, breaking cache scoping, quota, tool authorization,
and audit consistency.

CRITICAL: Downstream tests (P0-02 cache, P0-03 ERP authz) depend on this
identity contract being established first.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures: 共享 TestClient 工厂和 Mock 对象
# ═══════════════════════════════════════════════════════════════════════════════


def _make_mock_session_manager():
    """Create a fully-mocked SessionManager (mirrors test_api_routes.py)."""
    sm = MagicMock()
    sm.sessions = {}

    async def _create_session(sid=None):
        sid = sid or "auto-generated"
        sm.sessions[sid] = {
            "session_id": sid,
            "messages": [],
            "created_at": time.time(),
            "last_activity": time.time(),
            "message_count": 0,
            "user_id": None,
            "summary": "",
        }
        return sid

    sm.create_session = AsyncMock(side_effect=_create_session)
    sm.validate_session_token = MagicMock(return_value=True)
    sm.generate_session_token = MagicMock(return_value="mock-token-abc123")
    sm.set_user_id = MagicMock()
    return sm


def _make_mock_run_graph():
    """Mock run_graph that records call kwargs for assertion."""

    async def _run_graph(session_id, query, **kwargs):
        return {
            "response": f"Mock response for: {query}",
            "current_agent": "产品专家",
            "collaboration_mode": "sequential",
            "elapsed": 0.1,
            "cached": False,
            "agents_used": ["产品专家"],
            "resolution_status": "resolved",
        }

    return AsyncMock(side_effect=_run_graph)


def _build_app(sm=None, run_graph=None, dev_mode=True):
    """Build a minimal FastAPI app with chat + multimodal routes for testing."""
    from api.routes.chat import router as chat_router
    from api.routes.chat_multimodal import router as chat_multimodal_router

    app = FastAPI()
    app.include_router(chat_router)
    app.include_router(chat_multimodal_router)

    app.state.session_manager = sm or _make_mock_session_manager()
    app.state.run_graph = run_graph or _make_mock_run_graph()
    app.state.message_bus = MagicMock()
    app.state.dev_mode = dev_mode
    app.state.metrics = MagicMock()
    app.state._current_jwt_payload = None
    return app


# ═══════════════════════════════════════════════════════════════════════════════
# P0-04 Identity Propagation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSSEIdentityPropagation:
    """Authenticated user_id must reach run_graph for SSE and multimodal paths."""

    def setup_method(self):
        self.sm = _make_mock_session_manager()
        self.run_graph = _make_mock_run_graph()
        self.app = _build_app(sm=self.sm, run_graph=self.run_graph, dev_mode=True)
        self.client = TestClient(self.app)

    # ── SSE /api/chat/stream ──

    def test_sse_passes_authenticated_user_id_to_graph(self):
        """SSE /api/chat/stream: authenticated user_id reaches run_graph."""
        with (
            patch("api.routes.chat.extract_user_id", return_value="user-123"),
            patch("api.routes.chat.SSE_CHUNK_TIMEOUT", 2.0),
        ):
            resp = self.client.post(
                "/api/chat/stream",
                json={"query": "你好"},
            )
        assert resp.status_code == 200
        # Consume the SSE body so the async task completes
        _ = resp.text
        # Verify run_graph was called with the authenticated user_id
        assert self.run_graph.called, "run_graph was not called"
        call_kwargs = self.run_graph.call_args.kwargs
        assert "user_id" in call_kwargs, f"run_graph kwargs missing user_id. Got: {call_kwargs}"
        assert (
            call_kwargs["user_id"] == "user-123"
        ), f"Expected user_id='user-123', got: {call_kwargs['user_id']}"

    def test_sse_user_id_is_not_from_client_body(self):
        """SSE /api/chat/stream: client body 'user_id' cannot override auth."""
        with (
            patch("api.routes.chat.extract_user_id", return_value="user-123"),
            patch("api.routes.chat.SSE_CHUNK_TIMEOUT", 2.0),
        ):
            resp = self.client.post(
                "/api/chat/stream",
                json={"query": "你好", "user_id": "spoofed"},
            )
        assert resp.status_code == 200
        _ = resp.text
        assert self.run_graph.called, "run_graph was not called"
        call_kwargs = self.run_graph.call_args.kwargs
        assert "user_id" in call_kwargs, f"run_graph kwargs missing user_id. Got: {call_kwargs}"
        # The authenticated user_id must be used, not the spoofed body field
        assert call_kwargs["user_id"] == "user-123", (
            f"Authenticated identity overridden by client body. "
            f"Expected 'user-123', got: {call_kwargs['user_id']}"
        )

    def test_anonymous_sse_passes_no_user_id(self):
        """SSE /api/chat/stream: no auth → user_id=None (explicit anonymous)."""
        with (
            patch("api.routes.chat.extract_user_id", return_value=None),
            patch("api.routes.chat.SSE_CHUNK_TIMEOUT", 2.0),
        ):
            resp = self.client.post(
                "/api/chat/stream",
                json={"query": "你好"},
            )
        assert resp.status_code == 200
        _ = resp.text
        assert self.run_graph.called, "run_graph was not called"
        call_kwargs = self.run_graph.call_args.kwargs
        # Anonymous: user_id kwarg present but None
        assert "user_id" in call_kwargs, f"run_graph kwargs missing user_id. Got: {call_kwargs}"
        assert (
            call_kwargs["user_id"] is None
        ), f"Anonymous SSE should pass user_id=None, got: {call_kwargs['user_id']}"

    # ── Multimodal SSE /api/chat/multimodal/stream ──

    @patch("api.routes.chat_multimodal.MULTIMODAL_ENABLED", True)
    def test_multimodal_sse_preserves_user_id(self):
        """Multimodal SSE: authenticated user_id reaches run_graph."""
        with (
            patch(
                "api.routes.chat_multimodal._handle_image_upload", new_callable=AsyncMock
            ) as mock_img,
            patch("api.routes.chat.extract_user_id", return_value="user-123"),
            patch("api.routes.chat.SSE_CHUNK_TIMEOUT", 2.0),
        ):
            mock_img.return_value = [{"type": "text", "text": "analyze this"}]
            resp = self.client.post(
                "/api/chat/multimodal/stream",
                files={"image": ("pic.jpg", b"\xff\xd8\xff\xe0", "image/jpeg")},
            )
        assert resp.status_code == 200
        _ = resp.text
        assert self.run_graph.called, "run_graph was not called"
        call_kwargs = self.run_graph.call_args.kwargs
        assert (
            "user_id" in call_kwargs
        ), f"Multimodal SSE kwargs missing user_id. Got: {call_kwargs}"
        assert (
            call_kwargs["user_id"] == "user-123"
        ), f"Expected user_id='user-123', got: {call_kwargs['user_id']}"

    # ── REST /api/chat (identity parity baseline) ──

    def test_rest_identity_parity_baseline(self):
        """REST /api/chat already passes user_id (parity baseline)."""
        with patch("api.routes.chat.extract_user_id", return_value="user-123"):
            resp = self.client.post(
                "/api/chat",
                json={"query": "你好"},
            )
        assert resp.status_code == 200
        assert self.run_graph.called, "run_graph was not called"
        call_kwargs = self.run_graph.call_args.kwargs
        assert "user_id" in call_kwargs, f"REST kwargs missing user_id. Got: {call_kwargs}"

    # ── REST/SSE Identity Parity ──

    def test_rest_sse_identity_parity(self):
        """Same auth identity gets same user_id via REST and SSE."""
        # REST
        rest_run_graph = _make_mock_run_graph()
        app = _build_app(sm=self.sm, run_graph=rest_run_graph, dev_mode=True)
        client = TestClient(app)
        with patch("api.routes.chat.extract_user_id", return_value="parity-user"):
            resp = client.post("/api/chat", json={"query": "hello"})
        assert resp.status_code == 200
        rest_user_id = rest_run_graph.call_args.kwargs.get("user_id")

        # SSE
        sse_run_graph = _make_mock_run_graph()
        app2 = _build_app(sm=self.sm, run_graph=sse_run_graph, dev_mode=True)
        client2 = TestClient(app2)
        with (
            patch("api.routes.chat.extract_user_id", return_value="parity-user"),
            patch("api.routes.chat.SSE_CHUNK_TIMEOUT", 2.0),
        ):
            resp2 = client2.post("/api/chat/stream", json={"query": "hello"})
        assert resp2.status_code == 200
        _ = resp2.text
        sse_user_id = sse_run_graph.call_args.kwargs.get("user_id")

        assert (
            rest_user_id == sse_user_id
        ), f"Identity parity broken: REST user_id={rest_user_id}, SSE user_id={sse_user_id}"

    # ── Multimodal REST identity ──

    @patch("api.routes.chat_multimodal.MULTIMODAL_ENABLED", True)
    def test_multimodal_rest_preserves_user_id(self):
        """Multimodal REST /api/chat/image: authenticated user_id reaches run_graph."""
        with (
            patch(
                "api.routes.chat_multimodal._handle_image_upload", new_callable=AsyncMock
            ) as mock_img,
            patch("api.routes.chat.extract_user_id", return_value="user-123"),
        ):
            mock_img.return_value = [{"type": "text", "text": "analyze"}]
            resp = self.client.post(
                "/api/chat/image",
                files={"image": ("pic.jpg", b"\xff\xd8\xff\xe0", "image/jpeg")},
            )
        assert resp.status_code == 200
        assert self.run_graph.called, "run_graph was not called"
        call_kwargs = self.run_graph.call_args.kwargs
        assert (
            "user_id" in call_kwargs
        ), f"Multimodal REST kwargs missing user_id. Got: {call_kwargs}"
        assert (
            call_kwargs["user_id"] == "user-123"
        ), f"Expected user_id='user-123', got: {call_kwargs['user_id']}"


# ═══════════════════════════════════════════════════════════════════════════════
# _run_graph → Graph State Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunGraphIdentityPropagation:
    """Test that the run_graph function writes user_id into the graph state."""

    @pytest.mark.asyncio
    async def test_run_graph_writes_user_id_to_state(self):
        """_run_graph(user_id='user-123') writes state['user_id'] = 'user-123'."""
        from api.app import _run_graph

        mock_graph = AsyncMock()
        recorded_state = {}

        async def _capture_state(state, **kwargs):
            recorded_state.update(state)
            return {
                "response": "ok",
                "current_agent": "test",
                "collaboration_mode": "sequential",
                "cached": False,
                "agents_used": [],
                "resolution_status": "resolved",
            }

        mock_graph.ainvoke = AsyncMock(side_effect=_capture_state)
        mock_metrics = AsyncMock()
        mock_sla = AsyncMock()

        with (
            patch("api.app._graph_app", mock_graph),
            patch("api.app._session_manager", MagicMock()),
            patch("api.app._response_cache", MagicMock()),
            patch("api.app._metrics", mock_metrics),
            patch("api.app._sla_alert_mgr", mock_sla),
        ):
            user_id = "user-42"
            result = await _run_graph("test-session", "你好", user_id=user_id)
            assert result["response"] == "ok"
            # Verify the state passed to the actual graph app has user_id
            assert (
                "user_id" in recorded_state
            ), f"State missing user_id. State keys: {list(recorded_state.keys())}"
            assert (
                recorded_state["user_id"] == user_id
            ), f"Expected state['user_id']='{user_id}', got: {recorded_state['user_id']}"

    @pytest.mark.asyncio
    async def test_run_graph_writes_session_id_and_trace_id(self):
        """_run_graph writes session_id and trace_id into state."""
        from api.app import _run_graph

        mock_graph = AsyncMock()
        recorded_state = {}

        async def _capture_state(state, **kwargs):
            recorded_state.update(state)
            return {
                "response": "ok",
                "current_agent": "test",
                "collaboration_mode": "sequential",
                "cached": False,
                "agents_used": [],
                "resolution_status": "resolved",
            }

        mock_graph.ainvoke = AsyncMock(side_effect=_capture_state)
        mock_metrics = AsyncMock()

        with (
            patch("api.app._graph_app", mock_graph),
            patch("api.app._session_manager", MagicMock()),
            patch("api.app._response_cache", MagicMock()),
            patch("api.app._metrics", mock_metrics),
            patch("api.app._sla_alert_mgr", AsyncMock()),
        ):
            await _run_graph("sid-abc", "hello")
            assert recorded_state.get("session_id") == "sid-abc"
            assert "trace_id" in recorded_state, "State missing trace_id"


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket Identity Parity
# ═══════════════════════════════════════════════════════════════════════════════


def _make_mock_bus():
    """Create a fully-mocked MessageBus (mirrors test_api_routes.py)."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.subscribe = AsyncMock()
    bus.unsubscribe = AsyncMock()
    return bus


class TestWSIdentityPropagation:
    """Authenticated user_id from JWT must reach run_graph via WebSocket."""

    def test_websocket_passes_authenticated_user_id_to_graph(self):
        """WS /ws/chat: authenticated user_id from JWT sub reaches run_graph."""
        from api.routes.ws import router as ws_router

        run_graph = _make_mock_run_graph()
        app = FastAPI()
        app.include_router(ws_router)
        app.state.session_manager = _make_mock_session_manager()
        app.state.run_graph = run_graph
        app.state.message_bus = _make_mock_bus()
        app.state.dev_mode = True

        async def _mock_auth(ws, ws_api_key):
            return ("", "", {"sub": "ws-user-42"})

        with (
            patch("api.routes.ws._ws_authenticate", new_callable=AsyncMock, side_effect=_mock_auth),
            TestClient(app) as client,
            client.websocket_connect("/ws/chat") as ws,
        ):
            ws.send_json({"query": "hello"})
            # Drain messages until we see a response or hit a limit
            for _ in range(30):
                msg = ws.receive_json()
                if msg.get("type") == "response":
                    break

        assert run_graph.called, "run_graph was not called via WebSocket"
        call_kwargs = run_graph.call_args.kwargs
        assert "user_id" in call_kwargs, f"WebSocket kwargs missing user_id. Got: {call_kwargs}"
        assert (
            call_kwargs["user_id"] == "ws-user-42"
        ), f"Expected user_id='ws-user-42' from JWT sub, got: {call_kwargs['user_id']}"

    def test_rest_sse_ws_identity_parity(self):
        """Same auth identity gets same user_id via REST, SSE, and WebSocket."""
        from api.routes.ws import router as ws_router

        # -- REST --
        rest_run_graph = _make_mock_run_graph()
        rest_app = _build_app(sm=None, run_graph=rest_run_graph, dev_mode=True)
        with patch("api.routes.chat.extract_user_id", return_value="parity-user"):
            resp = TestClient(rest_app).post("/api/chat", json={"query": "hello"})
        assert resp.status_code == 200
        rest_user = rest_run_graph.call_args.kwargs.get("user_id")

        # -- SSE --
        sse_run_graph = _make_mock_run_graph()
        sse_app = _build_app(sm=None, run_graph=sse_run_graph, dev_mode=True)
        with (
            patch("api.routes.chat.extract_user_id", return_value="parity-user"),
            patch("api.routes.chat.SSE_CHUNK_TIMEOUT", 2.0),
        ):
            resp2 = TestClient(sse_app).post("/api/chat/stream", json={"query": "hello"})
        assert resp2.status_code == 200
        _ = resp2.text
        sse_user = sse_run_graph.call_args.kwargs.get("user_id")

        # -- WebSocket --
        ws_run_graph = _make_mock_run_graph()
        ws_app = FastAPI()
        ws_app.include_router(ws_router)
        ws_app.state.session_manager = _make_mock_session_manager()
        ws_app.state.run_graph = ws_run_graph
        ws_app.state.message_bus = _make_mock_bus()
        ws_app.state.dev_mode = True

        async def _mock_auth(ws, ws_api_key):
            return ("", "", {"sub": "parity-user"})

        with (
            patch("api.routes.ws._ws_authenticate", new_callable=AsyncMock, side_effect=_mock_auth),
            TestClient(ws_app) as client,
            client.websocket_connect("/ws/chat") as ws,
        ):
            ws.send_json({"query": "hello"})
            for _ in range(30):
                msg = ws.receive_json()
                if msg.get("type") == "response":
                    break
        ws_user = ws_run_graph.call_args.kwargs.get("user_id")

        assert (
            rest_user == sse_user == ws_user
        ), f"Identity parity broken: REST={rest_user}, SSE={sse_user}, WS={ws_user}"
