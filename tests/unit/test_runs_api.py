"""异步 Run API 测试（POST /api/runs、GET /api/runs/{run_id}）。

使用最小 FastAPI app + 注入的 RunService，dispatch 被替换为记录器，验证：
  - POST 立即返回 202 + run_id（不等待 Graph 完成）
  - GET 返回状态/attempt/result
  - Idempotency-Key 不重复创建/入队
  - ownership 校验
  - 入队失败 -> 503 + DLQ evidence
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.runs import IDEMPOTENCY_KEY_INPUT_MAX, router
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService, build_idempotency_scope
from runtime.statuses import RunStatus
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory


@pytest.fixture
def client(monkeypatch):
    session_factory, engine, path = make_sqlite_session_factory()
    service = RunService(repository=AgentRunRepository(session_factory=session_factory))

    calls: list[str] = []

    async def fake_dispatch(run_id: str, countdown: float | None = None):
        calls.append(run_id)
        return f"task-{len(calls)}"

    monkeypatch.setattr("runtime.dispatch.dispatch_run", fake_dispatch)

    app = FastAPI()
    app.state.run_service = service
    app.state.dev_mode = True
    app.state.session_manager = None
    app.include_router(router)

    with TestClient(app) as c:
        c.service = service
        c.dispatch_calls = calls
        yield c
    dispose(engine, path)


@pytest.mark.unit
def test_post_run_returns_immediately_queued(client):
    resp = client.post("/api/runs", json={"query": "帮我查订单", "session_id": "T-api-1"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["run_id"]
    assert body["status"] == RunStatus.QUEUED.value
    assert body["thread_id"] == "T-api-1"
    assert client.dispatch_calls == [body["run_id"]]

    # GET 可查询状态
    got = client.get(f"/api/runs/{body['run_id']}")
    assert got.status_code == 200
    detail = got.json()
    assert detail["status"] == RunStatus.QUEUED.value
    assert detail["attempt"] == 0
    assert detail["result"] is None


@pytest.mark.unit
def test_post_run_idempotency_key_does_not_duplicate(client):
    headers = {"Idempotency-Key": "same-key"}
    first = client.post("/api/runs", json={"query": "q", "session_id": "T-api-2"}, headers=headers)
    second = client.post("/api/runs", json={"query": "q", "session_id": "T-api-2"}, headers=headers)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    assert client.dispatch_calls == [first.json()["run_id"]]


@pytest.mark.unit
def test_idempotency_header_over_limit_rejected_before_db(client):
    """Header form shares the body's limit and is rejected with 400, not a DB 500."""
    key = "x" * (IDEMPOTENCY_KEY_INPUT_MAX + 1)
    resp = client.post(
        "/api/runs",
        json={"query": "q", "session_id": "T-api-len-h"},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 400
    assert client.dispatch_calls == []


@pytest.mark.unit
def test_idempotency_body_over_limit_rejected(client):
    """Body form is bounded by the same constant (pydantic -> 422)."""
    key = "x" * (IDEMPOTENCY_KEY_INPUT_MAX + 1)
    resp = client.post(
        "/api/runs",
        json={"query": "q", "session_id": "T-api-len-b", "idempotency_key": key},
    )
    assert resp.status_code in (400, 422)
    assert client.dispatch_calls == []


@pytest.mark.unit
def test_idempotency_key_at_limit_is_accepted(client):
    key = "x" * IDEMPOTENCY_KEY_INPUT_MAX
    resp = client.post(
        "/api/runs",
        json={"query": "q", "session_id": "T-api-len-ok"},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 202


@pytest.mark.unit
def test_scoped_idempotency_key_never_overflows_column():
    """Scoped key (user + endpoint + raw) is hashed when it exceeds the column."""
    long_key = "x" * IDEMPOTENCY_KEY_INPUT_MAX
    scoped = build_idempotency_scope("u-1", "POST:/api/runs", long_key)
    assert len(scoped) == 64
    assert scoped == build_idempotency_scope("u-1", "POST:/api/runs", long_key)
    assert scoped != build_idempotency_scope("u-2", "POST:/api/runs", long_key)


@pytest.mark.unit
def test_get_run_not_found(client):
    resp = client.get("/api/runs/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.unit
def test_get_run_ownership_enforced(client):
    # 直接创建带 owner 的 run；请求未携带匹配 JWT -> 403
    run = client.service.create_run(query="q", session_id="T-api-3", user_id="owner-1")
    client.app.state.dev_mode = False
    resp = client.get(f"/api/runs/{run['id']}")
    assert resp.status_code == 403


@pytest.mark.unit
def test_dispatch_failure_returns_503_and_dead_letter(client, monkeypatch):
    async def broken_dispatch(run_id: str, countdown: float | None = None):
        raise RuntimeError("broker down")

    monkeypatch.setattr("runtime.dispatch.dispatch_run", broken_dispatch)
    resp = client.post("/api/runs", json={"query": "q", "session_id": "T-api-4"})
    assert resp.status_code == 503

    # run 已被记录为 DEAD_LETTER，并有可查询 DLQ evidence
    dead = client.service.list_dead_letters()
    assert len(dead) == 1
    assert dead[0]["error_code"] == "DISPATCH_FAILED"
