"""Offline admission regressions; no provider calls or production certification."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.industrial import industrial_enabled, validate_industrial_profile
from core.session import ownership
from runtime.bootstrap import CheckpointEpisodeMismatch, inspect_episode_checkpoint
from runtime.errors import TransientError
from runtime.run_service import RunIdempotencyConflict, verify_idempotent_request


def test_industrial_rejects_mock_and_unprotected_runtime():
    failures = validate_industrial_profile({"ERP_MODE": "mock", "DEV_MODE": True})
    assert any("ERP_MODE=real" in item for item in failures)
    assert any("DEV_MODE=False" in item for item in failures)
    assert any("HITL_ENABLED=True" in item for item in failures)


def test_standard_profile_is_valid_and_does_not_enable_industrial_mode(monkeypatch):
    """Compose's standard profile must be accepted without industrial policy."""
    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "standard")

    assert industrial_enabled() is False


@pytest.mark.asyncio
async def test_industrial_owner_cannot_be_adopted(monkeypatch):
    records = {}

    class RedisFixture:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def set(self, key, value, *, nx, ex):
            assert nx and ex > 0
            if key in records:
                return False
            records[key] = value
            return True

        async def get(self, key):
            return records.get(key)

    monkeypatch.setattr(ownership, "_client", RedisFixture)
    sid = await ownership.create_owned_session("customer-a")
    await ownership.require_session_owner(sid, "customer-a")
    with pytest.raises(ownership.SessionOwnerDenied):
        await ownership.require_session_owner(sid, "customer-b")
    with pytest.raises(ownership.SessionOwnerDenied):
        await ownership.require_session_owner("legacy-session", "customer-a")


@pytest.mark.asyncio
async def test_industrial_owner_read_failure(monkeypatch):
    def unavailable():
        raise OSError("fixture unavailable")

    monkeypatch.setattr(ownership, "_client", unavailable)
    with pytest.raises(ownership.SessionOwnerUnavailable):
        await ownership.require_session_owner("session", "customer-a")


@pytest.mark.asyncio
async def test_strict_fast_path_refuses_unidentified_pending_episode():
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(next=("tool",), values={}))
    )
    with pytest.raises(CheckpointEpisodeMismatch):
        await inspect_episode_checkpoint(graph, {}, None, strict=True)
    graph.aget_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_strict_fast_path_refuses_failed_checkpoint_inspection():
    graph = SimpleNamespace(aget_state=AsyncMock(side_effect=OSError("fixture unavailable")))
    with pytest.raises(TransientError):
        await inspect_episode_checkpoint(graph, {}, None, strict=True)


def test_idempotency_key_cannot_represent_a_different_request():
    saved = {"query": "refund", "session_id": "s", "user_id": "u"}
    verify_idempotent_request(saved, query="refund", session_id="s", user_id="u")
    with pytest.raises(RunIdempotencyConflict):
        verify_idempotent_request(saved, query="other", session_id="s", user_id="u")
    with pytest.raises(RunIdempotencyConflict):
        verify_idempotent_request(saved, query="refund", session_id="other", user_id="u")
