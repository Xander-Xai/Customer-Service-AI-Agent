"""HITL 审批 API 测试：RBAC + approve/reject + resume + 幂等。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.app import create_app
from core.hitl.approval_service import ApprovalService
from db.models import Base
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.statuses import RunStatus


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    run_service = RunService(AgentRunRepository(session_factory))
    approval_service = ApprovalService(session_factory)

    sm = MagicMock()
    sm.validate_session_token = MagicMock(return_value=True)
    app = create_app(None, session_manager=sm)
    app.state.session_manager = sm
    app.state.dev_mode = True
    app.state.run_service = run_service
    app.state.approval_service = approval_service
    app.state._current_jwt_payload = None
    client = TestClient(app)
    return client, run_service, approval_service


def _waiting_run(run_service, approval_service, *, user_id="cust-1"):
    run = run_service.create_run(
        query="refund please", session_id="s1", thread_id="T1", user_id=user_id
    )
    rid = run["id"]
    run_service.mark_queued(rid)
    run_service.mark_running(rid)
    run_service.mark_waiting_approval(rid)
    approval = approval_service.create_or_get(
        run_id=rid,
        thread_id="T1",
        action="refund",
        risk_level="high",
        proposal={"order": "O1", "amount": 500},
        user_id=user_id,
        agent="billing",
    )
    return rid, approval["approval_id"]


def _reviewer(role="supervisor", username="sup-1"):
    user = MagicMock()
    user.role = role
    user.username = username
    user.id = username
    return user


@pytest.mark.unit
def test_unauthorized_reviewer_cannot_list(ctx):
    client, _run_service, _approval = ctx
    with patch("api.utils.check_admin_token", return_value=False), patch(
        "auth.router.require_auth", return_value=_reviewer(role="customer", username="cust")
    ):
        resp = client.get("/api/approvals")
    assert resp.status_code == 403


@pytest.mark.unit
def test_approve_resumes_run(ctx):
    client, run_service, approval_service = ctx
    rid, aid = _waiting_run(run_service, approval_service)
    with patch("api.utils.check_admin_token", return_value=False), patch(
        "auth.router.require_auth", return_value=_reviewer()
    ), patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")) as dispatch:
        resp = client.post(f"/api/approvals/{aid}/approve", json={"reason": "ok"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "APPROVED"
    assert run_service.get_run(rid)["status"] == RunStatus.QUEUED.value
    dispatch.assert_awaited_once_with(rid)


@pytest.mark.unit
def test_reject_does_not_resume_execution_but_requeues(ctx):
    client, run_service, approval_service = ctx
    rid, aid = _waiting_run(run_service, approval_service)
    with patch("api.utils.check_admin_token", return_value=False), patch(
        "auth.router.require_auth", return_value=_reviewer()
    ), patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")):
        resp = client.post(f"/api/approvals/{aid}/reject", json={"reason": "policy"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    # 拒绝也要恢复 graph 以收尾（由闸门保证不执行工具）
    assert run_service.get_run(rid)["status"] == RunStatus.QUEUED.value


@pytest.mark.unit
def test_duplicate_approve_is_idempotent(ctx):
    client, run_service, approval_service = ctx
    _rid, aid = _waiting_run(run_service, approval_service)
    with patch("api.utils.check_admin_token", return_value=False), patch(
        "auth.router.require_auth", return_value=_reviewer()
    ), patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")) as dispatch:
        first = client.post(f"/api/approvals/{aid}/approve")
        second = client.post(f"/api/approvals/{aid}/approve")
    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "APPROVED"
    dispatch.assert_awaited_once()  # 第二次不再 dispatch


@pytest.mark.unit
def test_reviewer_cannot_approve_own_action(ctx):
    client, run_service, approval_service = ctx
    # run 归属 "sup-1"，reviewer 也是 "sup-1"
    _rid, aid = _waiting_run(run_service, approval_service, user_id="sup-1")
    with patch("api.utils.check_admin_token", return_value=False), patch(
        "auth.router.require_auth", return_value=_reviewer(username="sup-1")
    ), patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")):
        resp = client.post(f"/api/approvals/{aid}/approve")
    assert resp.status_code == 403


@pytest.mark.unit
def test_get_approval_not_found(ctx):
    client, _run_service, _approval = ctx
    with patch("api.utils.check_admin_token", return_value=False), patch(
        "auth.router.require_auth", return_value=_reviewer()
    ):
        resp = client.get("/api/approvals/nope")
    assert resp.status_code == 404
