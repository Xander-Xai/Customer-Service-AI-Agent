"""API → DB → worker executor 集成测试（不依赖 Redis/Celery broker）。

证明：
  1. POST /api/runs 只入队（202，QUEUED），payload 只有 run_id；
  2. worker 执行器从 DB 读取 run、执行 graph、写回 SUCCEEDED；
  3. GET /api/runs/{id} 能读到结果；
  4. API 进程不拥有执行生命周期（执行由 worker service 独立完成）。
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.app import create_app
from db.models import Base
from runtime.executor import execute_run
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.statuses import RunStatus
from runtime.thread_lock import NullThreadLock


class _FakeRuntime:
    async def run(self, *, thread_id, query, user_id=None):
        return {
            "response": f"worker 回复: {query}",
            "current_agent": "general_agent",
            "collaboration_mode": "sequential",
        }


@pytest.mark.unit
async def test_api_enqueue_then_worker_executes():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    service = RunService(AgentRunRepository(session_factory))

    sm = AsyncMock()
    sm.validate_session_token = lambda *a, **k: True
    sm.generate_session_token = lambda *a, **k: "tok"

    app = create_app(None, session_manager=sm)
    app.state.session_manager = sm
    app.state.dev_mode = True
    app.state.run_service = service
    app.state._current_jwt_payload = None
    client = TestClient(app)

    captured: list[str] = []

    async def _capture_dispatch(run_id: str):
        captured.append(run_id)
        return "celery"

    with patch("runtime.dispatch.dispatch_run", new=_capture_dispatch):
        resp = client.post("/api/runs", json={"query": "精华液多少钱", "session_id": "it-1"})

    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert captured == [run_id]
    assert service.get_run(run_id)["status"] == RunStatus.QUEUED.value

    # worker 侧执行（模拟 Celery worker 消费 run_id）
    fake = _FakeRuntime()

    async def _provider():
        return fake

    final = await execute_run(run_id, service=service, lock_manager=NullThreadLock(), runtime_provider=_provider)
    assert final == RunStatus.SUCCEEDED.value

    got = client.get(f"/api/runs/{run_id}")
    assert got.status_code == 200
    assert got.json()["status"] == RunStatus.SUCCEEDED.value
    assert "worker 回复" in got.json()["result"]["response"]
