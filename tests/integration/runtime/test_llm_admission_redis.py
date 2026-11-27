"""Real Redis acceptance for the atomic provider/model token bucket."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import redis.asyncio as redis

from llm.admission import LLMAdmissionRejected, admit_provider_attempt

REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()
pytestmark = pytest.mark.skipif(not REDIS_URL, reason="requires real Redis via TEST_REDIS_URL")


@pytest.mark.asyncio
async def test_independent_clients_share_atomic_rpm_and_tpm(monkeypatch):
    from core import config

    monkeypatch.setattr("core.industrial.industrial_enabled", lambda: True)
    monkeypatch.setattr(config, "JWT_SECRET", "llm-admission-test-secret")
    monkeypatch.setattr(config, "REDIS_URL", REDIS_URL)
    monkeypatch.setenv("CSAI_LLM_RPM", "3")
    monkeypatch.setenv("CSAI_LLM_TPM", "120")
    monkeypatch.setenv("CSAI_LLM_USER_RPM", "20")
    monkeypatch.setenv("CSAI_LLM_USER_TPM", "120")
    monkeypatch.setenv("CSAI_LLM_BUCKET_TTL_SECONDS", "120")
    clients = [redis.Redis.from_url(REDIS_URL), redis.Redis.from_url(REDIS_URL)]
    unique_model = f"admission-test-{uuid.uuid4().hex}"
    try:
        decisions = await asyncio.gather(
            *(attempt(index, unique_model, clients[index % 2]) for index in range(12))
        )
        assert decisions.count(True) == 3
        assert decisions.count(False) == 9
    finally:
        scope = (
            __import__("hashlib")
            .sha256(("https://provider.example/v1\0" + unique_model).encode())
            .hexdigest()[:32]
        )
        keys = [
            key async for key in clients[0].scan_iter(match=f"csai:{{llm-admission:{scope}}}:*")
        ]
        if keys:
            await clients[0].delete(*keys)
        await asyncio.gather(*(client.aclose() for client in clients))


@pytest.mark.asyncio
async def test_request_larger_than_bucket_is_rejected_without_partial_debit(monkeypatch):
    from core import config

    monkeypatch.setattr("core.industrial.industrial_enabled", lambda: True)
    monkeypatch.setattr(config, "JWT_SECRET", "llm-admission-oversize-secret")
    monkeypatch.setattr(config, "REDIS_URL", REDIS_URL)
    monkeypatch.setenv("CSAI_LLM_RPM", "20")
    monkeypatch.setenv("CSAI_LLM_TPM", "9")
    monkeypatch.setenv("CSAI_LLM_USER_RPM", "20")
    monkeypatch.setenv("CSAI_LLM_USER_TPM", "9")
    monkeypatch.setenv("CSAI_LLM_BUCKET_TTL_SECONDS", "120")
    client = redis.Redis.from_url(REDIS_URL)
    unique_model = f"admission-oversize-{uuid.uuid4().hex}"
    try:
        with pytest.raises(LLMAdmissionRejected) as rejected:
            await admit_provider_attempt(
                base_url="https://provider.example/v1",
                model=unique_model,
                user_id="user-a",
                estimated_tokens=10,
                client_override=client,
            )
        assert rejected.value.retry_after is None
        await admit_provider_attempt(
            base_url="https://provider.example/v1",
            model=unique_model,
            user_id="user-a",
            estimated_tokens=9,
            client_override=client,
        )
    finally:
        scope = (
            __import__("hashlib")
            .sha256(("https://provider.example/v1\0" + unique_model).encode())
            .hexdigest()[:32]
        )
        keys = [key async for key in client.scan_iter(match=f"csai:{{llm-admission:{scope}}}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def attempt(index: int, model: str, client) -> bool:
    try:
        await admit_provider_attempt(
            base_url="https://provider.example/v1",
            model=model,
            user_id=f"user-{index}",
            estimated_tokens=10,
            client_override=client,
        )
        return True
    except LLMAdmissionRejected:
        return False
