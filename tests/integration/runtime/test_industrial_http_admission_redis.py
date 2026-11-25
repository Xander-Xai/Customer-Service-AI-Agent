"""Real Redis concurrency test in the existing runtime-e2e lane."""

import asyncio
import os
import uuid

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_REDIS_URL"), reason="requires real Redis")
async def test_industrial_shared_http_admission(monkeypatch):
    import redis.asyncio as redis

    from api.middleware.industrial_rate_limit import admission_key, admit_http

    url = os.environ["TEST_REDIS_URL"]
    monkeypatch.setattr("core.config.REDIS_URL", url)
    monkeypatch.setattr("core.config.JWT_SECRET", "fixture")
    identity = f"fixture-{uuid.uuid4().hex}"
    key = admission_key(identity, "http", "fixture")
    login_key = admission_key(identity, "login", "fixture")
    async with redis.from_url(url, decode_responses=True) as client:
        try:
            requests = [admit_http(identity, "http", 5, 60) for _ in range(20)]
            decisions = await asyncio.gather(*requests)
            assert decisions.count(True) == 5
            assert decisions.count(False) == 15
            assert await client.zcard(key) == 5
            assert 0 < await client.ttl(key) <= 60
            assert await admit_http(identity, "login", 1, 60) is True
            assert await admit_http(identity, "login", 1, 60) is False
        finally:
            await client.delete(key, login_key)
