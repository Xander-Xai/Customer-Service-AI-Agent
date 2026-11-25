"""HTTP admission wiring and Redis failure behavior, no external calls."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.parametrize(("decision", "status"), [(True, 200), (False, 429), (None, 503)])
def test_industrial_http_admission_never_falls_back(monkeypatch, decision, status):
    import api.middleware as middleware
    from api.middleware import industrial_rate_limit

    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "industrial")
    monkeypatch.setattr(middleware, "DEV_MODE", True)
    admit = AsyncMock(return_value=decision)
    monkeypatch.setattr(industrial_rate_limit, "admit_http", admit)
    legacy = MagicMock(side_effect=AssertionError("legacy limiter reached"))
    monkeypatch.setattr(middleware, "_redis_rate_limit", legacy)
    app = FastAPI()

    @app.post("/api/auth/login")
    async def login():
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    middleware.setup_middleware(app)
    with TestClient(app) as client:
        assert client.post("/api/auth/login").status_code == status
        assert client.get("/api/health").status_code == 200
    admit.assert_awaited_once_with("testclient", "login", 5, 300)
    legacy.assert_not_called()


@pytest.mark.asyncio
async def test_industrial_redis_failure_returns_unavailable(monkeypatch):
    from api.middleware import industrial_rate_limit

    monkeypatch.setattr("core.config.JWT_SECRET", "fixture")
    monkeypatch.setattr(
        industrial_rate_limit.redis, "from_url", MagicMock(side_effect=OSError("fixture"))
    )
    assert await industrial_rate_limit.admit_http("test", "http", 2, 60) is None


def test_legacy_redis_limiter_uses_window_cardinality(monkeypatch):
    import api.middleware as middleware

    pipeline = MagicMock()
    pipeline.execute.return_value = [0, 1, 61, True]
    redis = MagicMock()
    redis.pipeline.return_value = pipeline
    monkeypatch.setattr(middleware, "get_redis_client", lambda: redis)
    assert middleware._redis_rate_limit("fixture", 60, 60) is False
    pipeline.zcard.assert_called_once_with("csai:rate:fixture")
