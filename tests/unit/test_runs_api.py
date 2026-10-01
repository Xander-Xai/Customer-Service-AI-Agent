"""异步 Agent Run API 测试（POST 202 / GET result / cancel / 解耦）。

使用独立 SQLite + patched dispatch，不需要 Redis/Celery broker。
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.app import create_app
from db.models import Base
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.statuses import RunStatus


@pytest.fixture()
def run_ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    service = RunService(AgentRunRepository(session_factory))

    sm = MagicMock()
    sm.validate_session_token = MagicMock(return_value=True)
    sm.generate_session_token = MagicMock(return_value="tok")

    app = create_app(None, session_manager=sm)
    app.state.session_manager = sm
    app.state.dev_mode = True
    app.state.run_service = service
    app.state._current_jwt_payload = None
    client = TestClient(app)
    return client, service, session_factory


@pytest.mark.unit
def test_create_run_returns_202(run_ctx):
    client, _service, _sf = run_ctx
    with patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")) as dispatch:
        resp = client.post(
            "/api/runs", json={"query": "你好", "session_id": "sess-1"}
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == RunStatus.QUEUED.value
    assert body["thread_id"] == "sess-1"
    assert body["run_id"]
    # 队列 payload 只有 run_id（不含 query/state）
    dispatch.assert_awaited_once_with(body["run_id"])


@pytest.mark.unit
def test_get_run_returns_result(run_ctx):
    client, service, _sf = run_ctx
    with patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")):
        resp = client.post("/api/runs", json={"query": "q", "session_id": "sess-2"})
    rid = resp.json()["run_id"]

    service.mark_running(rid)
    service.mark_succeeded(rid, {"response": "答案", "current_agent": "product_agent"})

    got = client.get(f"/api/runs/{rid}")
    assert got.status_code == 200
    data = got.json()
    assert data["status"] == RunStatus.SUCCEEDED.value
    assert data["result"]["response"] == "答案"


@pytest.mark.unit
def test_cancel_before_start(run_ctx):
    client, _service, _sf = run_ctx
    with patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")):
        resp = client.post("/api/runs", json={"query": "q", "session_id": "sess-3"})
    rid = resp.json()["run_id"]

    cancelled = client.post(f"/api/runs/{rid}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == RunStatus.CANCELLED.value


@pytest.mark.unit
def test_cancel_running_conflicts(run_ctx):
    client, service, _sf = run_ctx
    with patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")):
        resp = client.post("/api/runs", json={"query": "q", "session_id": "sess-4"})
    rid = resp.json()["run_id"]
    service.mark_running(rid)

    conflict = client.post(f"/api/runs/{rid}/cancel")
    assert conflict.status_code == 409


@pytest.mark.unit
def test_get_run_not_found(run_ctx):
    client, _service, _sf = run_ctx
    assert client.get("/api/runs/nope").status_code == 404


@pytest.mark.unit
def test_empty_query_rejected(run_ctx):
    client, _service, _sf = run_ctx
    resp = client.post("/api/runs", json={"query": "   ", "session_id": "sess-5"})
    assert resp.status_code == 400


@pytest.mark.unit
def test_api_restart_does_not_own_execution_lifecycle(run_ctx):
    """API 只创建+入队；执行由另一个进程（worker）完成，API 重启不影响。"""
    client, _service, session_factory = run_ctx
    with patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")):
        resp = client.post("/api/runs", json={"query": "q", "session_id": "sess-6"})
    rid = resp.json()["run_id"]

    # 模拟 worker 进程：独立 service 实例，仅共享数据库
    worker_service = RunService(AgentRunRepository(session_factory))
    worker_service.mark_running(rid)
    worker_service.mark_succeeded(rid, {"response": "worker 产出"})

    # API 侧（即使重启）通过 DB 读取结果
    got = client.get(f"/api/runs/{rid}")
    assert got.json()["result"]["response"] == "worker 产出"

    # 结构性保证：API 路由不引用/执行 worker 任务
    import api.routes.runs as runs_module

    assert "execute_agent_run" not in inspect.getsource(runs_module)


@pytest.mark.unit
def test_duplicate_http_request_returns_same_run(run_ctx):
    """同一 Idempotency-Key（user+endpoint 作用域）只创建一个 run。"""
    client, _service, _sf = run_ctx
    with patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")) as dispatch:
        first = client.post(
            "/api/runs",
            json={"query": "退款", "session_id": "sess-idem"},
            headers={"Idempotency-Key": "req-123"},
        )
        second = client.post(
            "/api/runs",
            json={"query": "退款", "session_id": "sess-idem"},
            headers={"Idempotency-Key": "req-123"},
        )
    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    # 第二次幂等命中，不再入队
    dispatch.assert_awaited_once()


@pytest.mark.unit
def test_dead_runs_admin_endpoint(run_ctx):
    """管理员观测端点返回 DEAD run（脱敏）。"""
    client, service, _sf = run_ctx
    with patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")):
        resp = client.post("/api/runs", json={"query": "q", "session_id": "sess-dead"})
    rid = resp.json()["run_id"]
    service.mark_running(rid)
    service.mark_failed(rid, error_code="PermanentError", error_message="bad input", error_type="permanent")
    service.mark_dead(rid, error_code="PermanentError", error_message="bad input", error_type="permanent")

    with patch("api.routes.monitoring._require_monitoring_auth", return_value=None):
        dead = client.get("/api/runs/dead")
    assert dead.status_code == 200
    body = dead.json()
    assert body["count"] >= 1
    assert any(r["run_id"] == rid and r["status"] == RunStatus.DEAD.value for r in body["dead"])
