"""人工审批 API 测试：RBAC + 职责分离 + 决策幂等 + TTL。

API 层不是唯一防线（service 层已强制 SoD 与 TTL），但它是**对外暴露**的那一层，
因此必须自己也不漏：

- ``customer`` / ``agent`` -> 403（永无审批权）
- 未认证 -> 401
- 拿不到审批人身份 -> 401（**不允许**退化成固定 reviewer_id）
- 自审 -> 403
- 已过期 -> 409（不会被当成批准）
- 重复决策 -> 幂等，不改状态
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.approvals import router
from core.hitl.approval_service import (
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_REJECT,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ApprovalService,
    reset_approval_service_for_tests,
)
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory


class _User:
    def __init__(self, username: str, role: str):
        self.username = username
        self.role = role


@pytest.fixture
def env(monkeypatch):
    session_factory, engine, path = make_sqlite_session_factory()
    service = ApprovalService(session_factory=session_factory)
    reset_approval_service_for_tests()

    import core.hitl.approval_service as mod

    monkeypatch.setattr(mod, "_default_service", service, raising=False)

    dispatched: list[str] = []

    async def fake_dispatch(run_id: str, countdown: float | None = None):
        dispatched.append(run_id)
        return "task-1"

    monkeypatch.setattr("runtime.dispatch.dispatch_run", fake_dispatch)

    # 认证桩：按请求头决定返回什么 user
    def _auth(request):
        role = request.headers.get("X-Test-Role")
        if role is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        return _User(request.headers.get("X-Test-User", "someone"), role)

    monkeypatch.setattr("auth.router.require_auth", _auth)
    monkeypatch.setattr("api.utils.check_admin_token", lambda request: False)
    # 再把 admin token 本身清空：即使别的测试改过 check_admin_token 的实现，
    # admin-token 这条旁路也不可能通过（否则角色检查会被整段跳过）。
    import api.utils as _au

    monkeypatch.setattr(_au, "MONITORING_ADMIN_TOKEN", "", raising=False)

    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        client.service = service
        client.dispatched = dispatched
        yield client

    reset_approval_service_for_tests()
    dispose(engine, path)


def _new(env, **overrides) -> dict:
    payload = {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "action": "staging_refund",
        "risk_level": "high",
        "proposal": {"order_id": "O-1", "amount": 100},
        "user_id": "customer-1",
    }
    payload.update(overrides)
    return env.service.create_or_get(**payload)


def _sup(user: str = "supervisor-1") -> dict[str, str]:
    return {"X-Test-Role": "supervisor", "X-Test-User": user}


class TestRBAC:
    @pytest.mark.parametrize("role", ["customer", "agent"])
    def test_low_roles_are_forbidden(self, env, role):
        rec = _new(env)
        resp = env.get(
            f"/api/approvals/{rec['approval_id']}",
            headers={"X-Test-Role": role, "X-Test-User": f"{role}-1"},
        )
        assert resp.status_code == 403
        assert "需要管理员或主管权限" in resp.json()["detail"]

    @pytest.mark.parametrize("role", ["customer", "agent"])
    def test_low_roles_cannot_decide(self, env, role):
        rec = _new(env)
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_APPROVE},
            headers={"X-Test-Role": role, "X-Test-User": "customer-1"},
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("role", ["admin", "supervisor"])
    def test_high_roles_pass(self, env, role):
        rec = _new(env)
        resp = env.get(
            f"/api/approvals/{rec['approval_id']}",
            headers={"X-Test-Role": role, "X-Test-User": f"{role}-1"},
        )
        assert resp.status_code == 200

    def test_unauthenticated_is_401(self, env):
        rec = _new(env)
        assert env.get(f"/api/approvals/{rec['approval_id']}").status_code == 401


class TestReviewerIdentity:
    def test_missing_identity_is_401_not_a_default_reviewer(self, env):
        """身份不可为空——否则所有决策会共享同一个 reviewer_id，审计失去意义。"""
        rec = _new(env)
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_APPROVE},
            headers={"X-Test-Role": "admin", "X-Test-User": "   "},
        )
        assert resp.status_code == 401
        assert "审批人身份" in resp.json()["detail"]
        assert env.service.require(rec["approval_id"])["status"] == STATUS_PENDING


class TestSeparationOfDuties:
    def test_self_approval_is_403(self, env):
        """发起人即使拿到 supervisor 身份也不能审批自己的操作。"""
        rec = _new(env, user_id="boss")
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_APPROVE},
            headers=_sup("boss"),
        )
        assert resp.status_code == 403
        assert env.service.require(rec["approval_id"])["status"] == STATUS_PENDING

    def test_self_rejection_is_also_403(self, env):
        """自审的拒绝同样被拒：绕过它就能「自己拒自己的单」来取消执行。"""
        rec = _new(env, user_id="boss2")
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_REJECT},
            headers=_sup("boss2"),
        )
        assert resp.status_code == 403

    def test_other_reviewer_succeeds(self, env):
        rec = _new(env, user_id="customer-1")
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_APPROVE, "reason": "ok"},
            headers=_sup("supervisor-9"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["newly_decided"] is True
        assert body["approval"]["status"] == STATUS_APPROVED
        assert body["approval"]["reviewer_id"] == "supervisor-9"


class TestDecisions:
    def test_approve_dispatches_resume(self, env):
        rec = _new(env)
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_APPROVE},
            headers=_sup(),
        )
        body = resp.json()
        assert body["resume_dispatched"] is True
        assert env.dispatched == [rec["run_id"]]

    def test_reject_dispatches_resume(self, env):
        """拒绝也必须恢复：否则图一直挂在 interrupt 上。"""
        rec = _new(env)
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_REJECT, "reason": "not allowed"},
            headers=_sup(),
        )
        assert resp.json()["approval"]["status"] == STATUS_REJECTED
        assert env.dispatched == [rec["run_id"]]

    def test_edit(self, env):
        rec = _new(env)
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_EDIT, "edited_args": {"order_id": "O-1", "amount": 50}},
            headers=_sup(),
        )
        assert resp.status_code == 200
        assert resp.json()["approval"]["status"] == STATUS_APPROVED

    def test_edit_without_args_is_400(self, env):
        rec = _new(env)
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_EDIT},
            headers=_sup(),
        )
        assert resp.status_code == 400

    def test_unknown_decision_is_400(self, env):
        rec = _new(env)
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": "whatever"},
            headers=_sup(),
        )
        assert resp.status_code == 400

    def test_missing_approval_is_404(self, env):
        resp = env.post(
            "/api/approvals/nope/decision",
            json={"decision": DECISION_APPROVE},
            headers=_sup(),
        )
        assert resp.status_code == 404

    def test_duplicate_decision_is_idempotent(self, env):
        rec = _new(env)
        first = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_APPROVE},
            headers=_sup("sup-a"),
        )
        second = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_REJECT},
            headers=_sup("sup-b"),
        )
        assert first.json()["newly_decided"] is True
        assert second.json()["newly_decided"] is False
        # 首个决策不被覆盖，且不重复投递
        assert second.json()["approval"]["status"] == STATUS_APPROVED
        assert second.json()["approval"]["reviewer_id"] == "sup-a"
        assert env.dispatched == [rec["run_id"]]

    def test_dispatch_failure_does_not_rollback_decision(self, env, monkeypatch):
        """投递失败不回滚已落库的决策：审批记录不丢，执行仍受 ledger 保护。"""

        async def boom(run_id: str, countdown: float | None = None):
            raise ConnectionError("broker down")

        monkeypatch.setattr("runtime.dispatch.dispatch_run", boom)
        rec = _new(env)
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_APPROVE},
            headers=_sup(),
        )
        assert resp.status_code == 200
        assert resp.json()["resume_dispatched"] is False
        assert env.service.require(rec["approval_id"])["status"] == STATUS_APPROVED


class TestTTLAtApi:
    def test_expired_approval_is_409_not_approved(self, env, monkeypatch):
        """TTL 到期 -> 409。绝不能因为「没人处理」而默认放行。"""
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 1.0)
        rec = _new(env)

        import datetime as _dt

        real_utcnow = _dt.datetime.now(_dt.timezone.utc)

        class _Future:
            @staticmethod
            def now(tz=None):
                return real_utcnow + _dt.timedelta(seconds=120)

        monkeypatch.setattr(
            "core.hitl.approval_service._utcnow", staticmethod(lambda: _Future.now())
        )
        resp = env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_APPROVE},
            headers=_sup(),
        )
        assert resp.status_code == 409
        assert env.service.require(rec["approval_id"])["status"] == "EXPIRED"
        assert env.dispatched == []


class TestListing:
    def test_list_pending(self, env):
        _new(env, proposal={"order_id": "A"})
        _new(env, proposal={"order_id": "B"})
        resp = env.get("/api/approvals", headers=_sup())
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert {i["proposal"]["order_id"] for i in body["items"]} == {"A", "B"}

    def test_list_by_status(self, env):
        rec = _new(env, proposal={"order_id": "R"})
        _new(env, proposal={"order_id": "P"})
        env.post(
            f"/api/approvals/{rec['approval_id']}/decision",
            json={"decision": DECISION_REJECT},
            headers=_sup(),
        )
        resp = env.get("/api/approvals?status=REJECTED", headers=_sup())
        assert resp.json()["total"] == 1

    def test_list_by_run(self, env):
        _new(env, proposal={"order_id": "A"})
        _new(env, proposal={"order_id": "B"})
        resp = env.get("/api/approvals/by-run/run-1", headers=_sup())
        assert resp.json()["total"] == 2
        assert env.get("/api/approvals/by-run/other", headers=_sup()).json()["total"] == 0

    def test_listing_requires_permission(self, env):
        assert env.get("/api/approvals", headers={"X-Test-Role": "customer"}).status_code == 403

    def test_response_is_sanitized(self, env):
        """响应体不得回显未脱敏的凭据。"""
        rec = _new(env, proposal={"order_id": "O-1", "password": "hunter2"})
        resp = env.get(f"/api/approvals/{rec['approval_id']}", headers=_sup())
        assert "hunter2" not in resp.text
        assert resp.json()["proposal"]["order_id"] == "O-1"

    def test_response_exposes_expiry_flag(self, env, monkeypatch):
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 3600.0)
        rec = _new(env)
        body = env.get(f"/api/approvals/{rec['approval_id']}", headers=_sup()).json()
        assert body["expired"] is False
        assert body["expires_at"] is not None
