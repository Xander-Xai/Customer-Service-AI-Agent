from __future__ import annotations

import pytest

from llm.admission import (
    LLMAdmissionRejected,
    estimate_request_tokens,
)
from runtime.context import get_current_user_id, reset_run_context, set_run_context


def test_server_context_owns_llm_principal_and_resets_it():
    tokens = set_run_context("run-1", "thread-1", user_id="trusted-user")
    try:
        assert get_current_user_id() == "trusted-user"
    finally:
        reset_run_context(tokens)
    assert get_current_user_id() is None


def test_request_estimate_includes_prompt_and_reserved_output():
    estimate = estimate_request_tokens(
        [{"role": "user", "content": "商品订单查询"}], max_output_tokens=128
    )
    assert estimate >= 130


@pytest.mark.asyncio
async def test_client_rejects_before_transport_when_admission_denies(monkeypatch):
    import llm.admission as admission
    from llm.client import LLMServiceError, OpenAICompatibleClient

    async def deny(**kwargs):
        raise LLMAdmissionRejected(7)

    monkeypatch.setattr("core.industrial.industrial_enabled", lambda: True)
    monkeypatch.setattr(admission, "admit_provider_attempt", deny)
    monkeypatch.setattr(
        "core.token_quota.get_quota_manager",
        lambda: type("Quota", (), {"check_quota": lambda self, user: {"allowed": True}})(),
    )
    client = OpenAICompatibleClient("key", "http://provider.invalid/v1", "test-model")
    with pytest.raises(LLMServiceError, match="Retry-After=7s"):
        await client._admit_attempt(
            {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 10},
            "authenticated-user",
        )


def test_redis_quota_read_fails_closed_in_industrial_mode(monkeypatch):
    from core.token_quota import _RedisBackend

    class BrokenRedis:
        def hgetall(self, key):
            raise ConnectionError("redis unavailable")

    backend = _RedisBackend.__new__(_RedisBackend)
    backend._client = BrokenRedis()
    backend._prefix = "quota:"
    monkeypatch.setattr("core.industrial.industrial_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="Token Quota Redis read unavailable"):
        backend.get("customer-1")


def test_provider_model_bucket_keys_share_a_cluster_slot(monkeypatch):
    import llm.admission as admission

    keys = []

    class FakeRedis:
        async def eval(self, script, numkeys, *args):
            keys.extend(args[:numkeys])
            return [1, 0]

    async def run():
        monkeypatch.setattr("core.industrial.industrial_enabled", lambda: True)
        monkeypatch.setattr("core.config.JWT_SECRET", "test-secret")
        monkeypatch.setattr("core.config.REDIS_URL", "redis://unused")
        monkeypatch.setattr(admission, "_settings", lambda: (60, 120000, 6, 12000, 7200))
        monkeypatch.setattr(admission, "_client", FakeRedis())
        await admission.admit_provider_attempt(
            base_url="https://provider.example/v1",
            model="model-a",
            user_id="customer-1",
            estimated_tokens=42,
        )

    import asyncio

    asyncio.run(run())
    assert len(keys) == 2
    assert keys[0].split("}")[0] == keys[1].split("}")[0]
