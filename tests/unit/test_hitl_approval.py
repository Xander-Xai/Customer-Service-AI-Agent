"""durable 审批服务测试：状态机 / TTL / 职责分离 / 幂等。

这些是本能力的治理底线，逐条对应一个「如果失效会怎样」：

============================  ==========================================
不变量                        失效后果
============================  ==========================================
reviewer != requester          发起人自批自己的退款 = 治理形同虚设
TTL 到期 = EXPIRED（不放行）  「等太久就默认批准」= 无人值守的写操作
decide() 幂等                 重复提交造成状态抖动 / 审计混乱
create_or_get 幂等            重投递反复打扰审批人
consume_resume 只消费一次     同一决策被多次恢复 = 副作用重放
============================  ==========================================
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.hitl.approval_service import (
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_REJECT,
    STATUS_APPROVED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalService,
    InvalidApprovalDecision,
    SelfApprovalForbidden,
    is_expired,
)
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory


@pytest.fixture
def svc():
    factory, engine, path = make_sqlite_session_factory()
    try:
        yield ApprovalService(session_factory=factory)
    finally:
        dispose(engine, path)


def _create(svc, **overrides):
    payload = {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "action": "staging_refund",
        "risk_level": "high",
        "proposal": {"order_id": "O-1", "amount": 100},
        "user_id": "customer-1",
        "agent": "order_agent",
    }
    payload.update(overrides)
    return svc.create_or_get(**payload)


class TestCreateOrGet:
    def test_creates_pending_record(self, svc):
        rec = _create(svc)
        assert rec["status"] == STATUS_PENDING
        assert rec["action"] == "staging_refund"
        assert rec["user_id"] == "customer-1"
        assert rec["expires_at"] is not None

    def test_idempotent_on_same_proposal(self, svc):
        """同一 run + 同一动作 + 同一提案 => 复用同一条审批。"""
        first = _create(svc)
        second = _create(svc)
        assert first["approval_id"] == second["approval_id"]
        assert len(svc.list_by_run("run-1")) == 1

    def test_different_proposal_creates_new_record(self, svc):
        """换参数 = 换提案 => 新审批（不能借旧审批蒙混过关）。"""
        first = _create(svc)
        second = _create(svc, proposal={"order_id": "O-2", "amount": 100})
        assert first["approval_id"] != second["approval_id"]

    def test_different_action_creates_new_record(self, svc):
        first = _create(svc)
        second = _create(svc, action="staging_order_change")
        assert first["approval_id"] != second["approval_id"]

    def test_proposal_is_sanitized_on_write(self, svc):
        """落库的是脱敏后的提案——审批留痕不得成为凭据泄漏通道。"""
        rec = _create(svc, proposal={"order_id": "O-1", "password": "hunter2"})
        assert rec["proposal"]["order_id"] == "O-1"
        assert "hunter2" not in str(rec["proposal"])

    def test_fingerprint_is_stable_for_same_proposal(self, svc):
        a = _create(svc, proposal={"order_id": "O-9"})
        b = _create(svc, proposal={"order_id": "O-9"})
        assert a["proposal_fingerprint"] == b["proposal_fingerprint"]

    def test_ttl_derived_from_config(self, svc, monkeypatch):
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 120.0)
        rec = _create(svc, proposal={"order_id": "O-ttl"})
        expires = rec["expires_at"]
        assert expires is not None
        delta = (expires - rec["requested_at"]).total_seconds()
        assert 110 <= delta <= 130

    def test_ttl_disabled_when_non_positive(self, svc, monkeypatch):
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 0.0)
        rec = _create(svc, proposal={"order_id": "O-nottl"})
        assert rec["expires_at"] is None
        assert is_expired(rec) is False


class TestSeparationOfDuties:
    def test_self_approval_is_forbidden(self, svc):
        """发起人不得审批自己的操作。"""
        rec = _create(svc, user_id="customer-1")
        with pytest.raises(SelfApprovalForbidden):
            svc.decide(rec["approval_id"], reviewer_id="customer-1", decision=DECISION_APPROVE)

    def test_different_reviewer_allowed(self, svc):
        rec = _create(svc, user_id="customer-1")
        updated, newly = svc.decide(
            rec["approval_id"], reviewer_id="supervisor-9", decision=DECISION_APPROVE
        )
        assert newly is True
        assert updated["status"] == STATUS_APPROVED
        assert updated["reviewer_id"] == "supervisor-9"

    def test_system_triggered_allows_review(self, svc):
        """无已知发起人（user_id 为空）时不阻断：真正的边界是 API 的 RBAC。"""
        rec = _create(svc, user_id=None)
        updated, _ = svc.decide(
            rec["approval_id"], reviewer_id="supervisor-1", decision=DECISION_APPROVE
        )
        assert updated["status"] == STATUS_APPROVED

    def test_guard_applies_to_every_decision_kind(self, svc):
        for decision, extra in (
            (DECISION_APPROVE, {}),
            (DECISION_REJECT, {}),
            (DECISION_EDIT, {"edited_args": {"order_id": "O-1"}}),
        ):
            rec = _create(svc, user_id=f"cust-{decision}", proposal={"order_id": f"O-{decision}"})
            with pytest.raises(SelfApprovalForbidden):
                svc.decide(
                    rec["approval_id"],
                    reviewer_id=f"cust-{decision}",
                    decision=decision,
                    **extra,
                )


class TestDecisions:
    def test_approve(self, svc):
        rec = _create(svc)
        updated, newly = svc.decide(
            rec["approval_id"], reviewer_id="sup", decision=DECISION_APPROVE, reason="ok"
        )
        assert newly is True
        assert updated["status"] == STATUS_APPROVED
        assert updated["decision"]["decision"] == DECISION_APPROVE
        assert updated["reason"] == "ok"

    def test_reject(self, svc):
        rec = _create(svc)
        updated, _ = svc.decide(
            rec["approval_id"], reviewer_id="sup", decision=DECISION_REJECT, reason="no"
        )
        assert updated["status"] == STATUS_REJECTED

    def test_edit_records_edited_args(self, svc):
        rec = _create(svc)
        updated, _ = svc.decide(
            rec["approval_id"],
            reviewer_id="sup",
            decision=DECISION_EDIT,
            edited_args={"order_id": "O-1", "amount": 50},
        )
        assert updated["status"] == STATUS_APPROVED
        assert updated["decision"]["decision"] == DECISION_EDIT
        assert updated["decision"]["edited_args"]["amount"] == 50

    def test_edit_requires_edited_args(self, svc):
        rec = _create(svc)
        with pytest.raises(InvalidApprovalDecision):
            svc.decide(rec["approval_id"], reviewer_id="sup", decision=DECISION_EDIT)

    def test_edit_args_are_sanitized(self, svc):
        rec = _create(svc)
        updated, _ = svc.decide(
            rec["approval_id"],
            reviewer_id="sup",
            decision=DECISION_EDIT,
            edited_args={"order_id": "O-1", "api_key": "sk-live"},
        )
        assert "sk-live" not in str(updated["decision"]["edited_args"])

    def test_unknown_decision_rejected(self, svc):
        rec = _create(svc)
        with pytest.raises(InvalidApprovalDecision):
            svc.decide(rec["approval_id"], reviewer_id="sup", decision="maybe")

    def test_empty_decision_rejected(self, svc):
        rec = _create(svc)
        with pytest.raises(InvalidApprovalDecision):
            svc.decide(rec["approval_id"], reviewer_id="sup", decision="")

    def test_missing_reviewer_rejected(self, svc):
        """审批人标识不可为空——空标识会让职责分离与审计都失去意义。"""
        rec = _create(svc)
        with pytest.raises(InvalidApprovalDecision):
            svc.decide(rec["approval_id"], reviewer_id="  ", decision=DECISION_APPROVE)

    def test_unknown_approval_raises(self, svc):
        with pytest.raises(ApprovalNotFound):
            svc.decide("does-not-exist", reviewer_id="sup", decision=DECISION_APPROVE)

    def test_decide_is_idempotent(self, svc):
        """重复提交（双击 / 重试）必须是 no-op，不改状态。"""
        rec = _create(svc)
        first, newly_first = svc.decide(
            rec["approval_id"], reviewer_id="sup", decision=DECISION_APPROVE
        )
        second, newly_second = svc.decide(
            rec["approval_id"], reviewer_id="other", decision=DECISION_REJECT
        )
        assert newly_first is True
        assert newly_second is False
        assert second["status"] == first["status"] == STATUS_APPROVED
        assert second["reviewer_id"] == first["reviewer_id"]  # 首个决策不被覆盖


class TestTTL:
    def test_expired_approval_cannot_be_approved(self, svc, monkeypatch):
        """TTL 到期按拒绝处理，**绝不**默认放行。"""
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 1.0)
        rec = _create(svc)
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        with pytest.raises(ApprovalExpired):
            svc.decide(
                rec["approval_id"],
                reviewer_id="sup",
                decision=DECISION_APPROVE,
                now=later,
            )

    def test_expiry_is_persisted_as_expired(self, svc, monkeypatch):
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 1.0)
        rec = _create(svc)
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        with pytest.raises(ApprovalExpired):
            svc.decide(rec["approval_id"], reviewer_id="sup", decision=DECISION_APPROVE, now=later)
        stored = svc.require(rec["approval_id"])
        assert stored["status"] == STATUS_EXPIRED

    def test_expired_is_distinguishable_from_reject(self, svc, monkeypatch):
        """EXPIRED 可与「明确拒绝」区分统计。"""
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 1.0)
        rec = _create(svc, proposal={"order_id": "O-exp"})
        with pytest.raises(ApprovalExpired):
            svc.decide(
                rec["approval_id"],
                reviewer_id="sup",
                decision=DECISION_APPROVE,
                now=datetime.now(timezone.utc) + timedelta(seconds=10),
            )
        assert svc.require(rec["approval_id"])["status"] == STATUS_EXPIRED

    def test_explicit_expire_helper(self, svc, monkeypatch):
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 1.0)
        rec = _create(svc, proposal={"order_id": "O-h"})
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        assert svc.expire(rec["approval_id"], now=later) is True
        assert svc.require(rec["approval_id"])["status"] == STATUS_EXPIRED

    def test_expire_before_deadline_is_a_noop(self, svc, monkeypatch):
        """未到期不能被提前 expire：否则等于给了一个绕过 TTL 的拒绝入口。"""
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 3600.0)
        rec = _create(svc, proposal={"order_id": "O-early"})
        assert svc.expire(rec["approval_id"]) is False
        assert svc.require(rec["approval_id"])["status"] == STATUS_PENDING

    def test_expire_is_idempotent(self, svc, monkeypatch):
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 1.0)
        rec = _create(svc, proposal={"order_id": "O-i"})
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        assert svc.expire(rec["approval_id"], now=later) is True
        assert svc.expire(rec["approval_id"], now=later) is False

    def test_is_expired_helper(self, svc, monkeypatch):
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 60.0)
        rec = _create(svc, proposal={"order_id": "O-h2"})
        assert is_expired(rec) is False
        assert is_expired(rec, now=datetime.now(timezone.utc) + timedelta(seconds=120)) is True

    def test_list_pending_converges_expired(self, svc, monkeypatch):
        """读路径顺带把超时项落 EXPIRED，避免过期审批一直占队列。"""
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 1.0)
        _create(svc, proposal={"order_id": "O-pend"})
        later = datetime.now(timezone.utc) + timedelta(seconds=120)
        pending = svc.list_pending(now=later)
        assert all(p["status"] == STATUS_EXPIRED for p in pending)


class TestConsumeResume:
    def test_returns_none_when_still_pending(self, svc):
        _create(svc)
        assert svc.consume_resume("run-1") is None

    def test_returns_decision_after_approval(self, svc):
        rec = _create(svc)
        svc.decide(rec["approval_id"], reviewer_id="sup", decision=DECISION_APPROVE)
        payload = svc.consume_resume("run-1")
        assert payload is not None
        assert payload["decision"] == DECISION_APPROVE
        assert payload["approval_id"] == rec["approval_id"]
        assert payload["reviewer_id"] == "sup"

    def test_consumed_only_once(self, svc):
        """`WHERE resumed_at IS NULL` 原子认领：重投递不得重复消费同一决策。"""
        rec = _create(svc)
        svc.decide(rec["approval_id"], reviewer_id="sup", decision=DECISION_APPROVE)
        assert svc.consume_resume("run-1") is not None
        assert svc.consume_resume("run-1") is None

    def test_rejection_is_also_consumable(self, svc):
        """拒绝也必须能被消费——否则图会一直挂在 interrupt 上。"""
        rec = _create(svc)
        svc.decide(rec["approval_id"], reviewer_id="sup", decision=DECISION_REJECT)
        payload = svc.consume_resume("run-1")
        assert payload["decision"] == DECISION_REJECT

    def test_unknown_run_returns_none(self, svc):
        assert svc.consume_resume("no-such-run") is None

    def test_expired_pending_is_converged_and_treated_as_rejection(self, svc, monkeypatch):
        """超时的 PENDING 在消费路径落 EXPIRED 并作为拒绝返回，图不会无限等待。

        回归：过期收敛那条 UPDATE 已经原子写入了 ``resumed_at``，因此**不能**再走
        ``WHERE resumed_at IS NULL`` 的认领分支。历史上两段串在一起导致 rowcount
        必为 0 -> 永远返回 None -> 图一直挂在 interrupt 上（死等）。
        """
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 1.0)
        _create(svc)
        later = datetime.now(timezone.utc) + timedelta(seconds=120)
        payload = svc.consume_resume("run-1", now=later)
        assert payload is not None
        assert payload["decision"] == "expired"
        # 仍然只消费一次：第二次拿不到，避免重复恢复。
        assert svc.consume_resume("run-1", now=later) is None
        assert svc.list_by_run("run-1")[0]["status"] == STATUS_EXPIRED

    def test_unexpired_pending_is_not_converged(self, svc, monkeypatch):
        monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 3600.0)
        _create(svc)
        assert svc.consume_resume("run-1") is None
        assert svc.list_by_run("run-1")[0]["status"] == STATUS_PENDING


class TestListing:
    def test_list_by_run_is_ordered(self, svc):
        _create(svc, action="staging_refund", proposal={"order_id": "A"})
        _create(svc, action="staging_order_change", proposal={"order_id": "B"})
        rows = svc.list_by_run("run-1")
        assert [r["action"] for r in rows] == ["staging_refund", "staging_order_change"]

    def test_list_by_status_filters(self, svc):
        keep = _create(svc, proposal={"order_id": "K"})
        _create(svc, proposal={"order_id": "D"})
        svc.decide(keep["approval_id"], reviewer_id="sup", decision=DECISION_REJECT)
        pending = svc.list_pending()
        assert [r["proposal"]["order_id"] for r in pending] == ["D"]
        assert [r["proposal"]["order_id"] for r in svc.list_by_status(STATUS_REJECTED)] == ["K"]

    def test_require_raises_when_missing(self, svc):
        with pytest.raises(ApprovalNotFound):
            svc.require("nope")

    def test_get_returns_none_when_missing(self, svc):
        assert svc.get("nope") is None

    def test_pending_for_run(self, svc):
        _create(svc)
        assert len(svc.pending_for_run("run-1")) == 1
        assert svc.pending_for_run("other") == []
