"""Fast-path admission is on the real graph boundary; no network in unit tests."""

from unittest.mock import AsyncMock

import pytest

from core.concurrency.fast_path_admission import FastPathAdmissionError, execute_admitted


@pytest.mark.asyncio
async def test_missing_identity_never_runs_graph():
    graph = AsyncMock()
    with pytest.raises(FastPathAdmissionError) as error:
        await execute_admitted(None, graph)
    assert error.value.status == 503
    graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_deadline_never_runs_graph(monkeypatch):
    monkeypatch.setenv("CSAI_FAST_PATH_DEADLINE_SECONDS", "300")
    monkeypatch.setattr("core.config.AGENT_RUN_THREAD_LOCK_TTL_SECONDS", 300.0)
    graph = AsyncMock()
    with pytest.raises(FastPathAdmissionError) as error:
        await execute_admitted("owner", graph)
    assert error.value.status == 503
    graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_boundary_calls_admission_in_industrial_profile(monkeypatch):
    from api import app
    from core.concurrency import fast_path_admission

    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "industrial")
    graph = AsyncMock(return_value={"response": "ok"})
    monkeypatch.setattr(app, "_run_graph_inner", graph)

    async def rejected(user_id, operation):
        assert user_id == "owner"
        raise FastPathAdmissionError("FAST_PATH_USER_BUSY", 429)

    monkeypatch.setattr(fast_path_admission, "execute_admitted", rejected)
    with pytest.raises(FastPathAdmissionError) as error:
        await app._run_graph("fixture", "query", user_id="owner")
    assert error.value.status == 429
    graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_failure_refuses_operation(monkeypatch):
    from core.concurrency import fast_path_admission

    monkeypatch.setattr("core.config.JWT_SECRET", "fixture")
    monkeypatch.setenv("CSAI_FAST_PATH_DEADLINE_SECONDS", "90")
    monkeypatch.setattr("core.config.AGENT_RUN_THREAD_LOCK_TTL_SECONDS", 300.0)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.eval.side_effect = ConnectionError("fixture")
    monkeypatch.setattr(fast_path_admission.redis, "from_url", lambda *args, **kwargs: client)
    graph = AsyncMock()
    with pytest.raises(FastPathAdmissionError) as error:
        await execute_admitted("owner", graph)
    assert error.value.code == "FAST_PATH_UNAVAILABLE"
    graph.assert_not_awaited()
