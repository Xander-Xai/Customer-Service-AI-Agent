"""Human handoff HTTP contract: token, RBAC, claiming and customer reply polling."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.handoff_service import HandoffService
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory

pytestmark = pytest.mark.unit


class _User:
    def __init__(self, user_id, role):
        self.id = user_id
        self.role = role


class _SessionManager:
    def validate_session_token(self, sid, token):
        return token == "session-proof:" + sid

    def set_user_id(self, sid, user_id):
        return None


@pytest.fixture
def client(monkeypatch):
    from api.routes.handoffs import router

    factory, engine, path = make_sqlite_session_factory()
    svc = HandoffService(session_factory=factory)
    monkeypatch.setattr("core.config.HUMAN_HANDOFF_ENABLED", True)
    monkeypatch.setattr("api.routes.handoffs.get_handoff_service", lambda: svc)

    def auth(req):
        uid = req.headers.get("X-Test-User")
        role = req.headers.get("X-Test-Role")
        if not uid or not role:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="unauthenticated")
        return _User(uid, role)

    monkeypatch.setattr("auth.router.require_auth", auth)
    monkeypatch.setattr(
        "api.routes.chat.extract_user_id",
        lambda req: req.headers.get("X-Test-User"),
    )
    app = FastAPI()
    app.state.session_manager = _SessionManager()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client
    dispose(engine, path)


def _headers(user="customer-1", role="customer"):
    return {"X-Test-User": user, "X-Test-Role": role}


def _create(client, session="session-1"):
    return client.post(
        "/api/handoffs",
        json={
            "session_id": session,
            "session_token": "session-proof:" + session,
            "reason": "需要人工客服",
            "message": "我要向人工坐席说明订单问题",
        },
        headers=_headers(),
    )


def test_ticket_requires_customer_identity_and_session_proof(client):
    unauthorized = client.post(
        "/api/handoffs",
        json={
            "session_id": "session-1",
            "session_token": "wrong",
            "reason": "需要人工",
        },
        headers=_headers(),
    )
    assert unauthorized.status_code == 403
    assert _create(client).status_code == 200
    assert _create(client).json()["ticket_id"] == _create(client).json()["ticket_id"]
    as_staff = client.post(
        "/api/handoffs",
        json={
            "session_id": "session-2",
            "session_token": "session-proof:session-2",
            "reason": "request",
        },
        headers=_headers("agent-1", "agent"),
    )
    assert as_staff.status_code == 403


def test_end_to_end_handoff_customer_and_staff_roles(client):
    resp = _create(client)
    assert resp.status_code == 200
    tid = resp.json()["ticket_id"]

    assert client.get("/api/handoffs").status_code == 401
    assert client.get("/api/handoffs", headers=_headers()).status_code == 403
    assert client.get(f"/api/handoffs/{tid}", headers=_headers("customer-2")).status_code == 404
    queue = client.get("/api/handoffs?status=OPEN", headers=_headers("agent-1", "agent"))
    assert queue.status_code == 200
    assert len(queue.json()["tickets"]) == 1

    claimed = client.post(f"/api/handoffs/{tid}/claim", headers=_headers("agent-1", "agent"))
    assert claimed.status_code == 200
    assert claimed.json()["assigned_to"] == "agent-1"
    assert (
        client.post(f"/api/handoffs/{tid}/claim", headers=_headers("agent-2", "agent")).status_code
        == 409
    )

    payload = {"body": "已找到您的订单，将继续跟进。", "client_message_id": "reply-1"}
    reply = client.post(
        f"/api/handoffs/{tid}/messages",
        json=payload,
        headers=_headers("agent-1", "agent"),
    )
    assert reply.status_code == 200
    assert (
        client.post(
            f"/api/handoffs/{tid}/messages",
            json=payload,
            headers=_headers("agent-1", "agent"),
        ).json()["message_id"]
        == reply.json()["message_id"]
    )
    assert (
        client.get(f"/api/handoffs/{tid}/messages", headers=_headers()).json()["messages"][-1][
            "body"
        ]
        == payload["body"]
    )
    assert (
        client.post(
            f"/api/handoffs/{tid}/close",
            json={"resolution": "问题已核实并处理"},
            headers=_headers("customer-1", "customer"),
        ).status_code
        == 403
    )
    closed = client.post(
        f"/api/handoffs/{tid}/close",
        json={"resolution": "问题已核实并处理"},
        headers=_headers("agent-1", "agent"),
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "CLOSED"
    reopened = client.post(f"/api/handoffs/{tid}/reopen", headers=_headers())
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "OPEN"


def test_api_off_by_default(client, monkeypatch):
    monkeypatch.setattr("core.config.HUMAN_HANDOFF_ENABLED", False)
    assert client.get("/api/handoffs", headers=_headers("agent-1", "agent")).status_code == 503


def test_staff_task_episode_requires_claim_and_never_authorizes_execution(client):
    tid = _create(client).json()["ticket_id"]
    url = f"/api/handoffs/{tid}/task-episode"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=_headers()).status_code == 403
    staff = _headers("agent-1", "agent")
    assert client.get(url, headers=staff).status_code == 404
    assert client.post(f"/api/handoffs/{tid}/claim", headers=staff).status_code == 200
    result = client.get(url, headers=staff)
    assert result.status_code == 200
    assert result.json() == {"status": "NO_MATCHING_RUN", "execution_authorized": False}
    assert client.get(url, headers=_headers("agent-2", "agent")).status_code == 404
