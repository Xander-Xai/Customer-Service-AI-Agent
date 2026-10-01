"""HITL 审批服务测试（durable 记录 + 状态机 + 幂等）。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.hitl.approval_service import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ApprovalNotFound,
    ApprovalService,
    InvalidApprovalDecision,
)
from db.models import Base


@pytest.fixture()
def service():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return ApprovalService(session_factory)


def _request(service, **overrides):
    base = dict(
        run_id="run-1",
        thread_id="th-1",
        action="refund",
        risk_level="high",
        proposal={"order": "O1", "amount": 500},
        user_id="customer-1",
        agent="billing_agent",
    )
    base.update(overrides)
    return service.create_or_get(**base)


@pytest.mark.unit
def test_create_or_get_is_idempotent(service):
    a = _request(service)
    b = _request(service)
    assert a["approval_id"] == b["approval_id"]
    assert a["status"] == STATUS_PENDING
    assert len(service.list_by_run("run-1")) == 1


@pytest.mark.unit
def test_proposal_is_sanitized(service):
    record = _request(service, proposal={"order": "O1", "api_key": "SECRET"})
    assert "api_key" not in record["proposal"]


@pytest.mark.unit
def test_decide_approve_and_reject(service):
    a = _request(service, action="refund")
    record, newly = service.decide(
        a["approval_id"], reviewer_id="sup-1", decision="approve", reason="ok"
    )
    assert newly is True
    assert record["status"] == STATUS_APPROVED
    assert record["reviewer_id"] == "sup-1"

    b = _request(service, action="modify_order")
    record_b, newly_b = service.decide(b["approval_id"], reviewer_id="sup-1", decision="reject")
    assert newly_b is True
    assert record_b["status"] == STATUS_REJECTED


@pytest.mark.unit
def test_duplicate_decision_is_idempotent(service):
    a = _request(service)
    first, newly1 = service.decide(a["approval_id"], reviewer_id="sup-1", decision="approve")
    second, newly2 = service.decide(a["approval_id"], reviewer_id="sup-2", decision="reject")
    assert newly1 is True and newly2 is False
    # 不覆盖首次决策
    assert second["status"] == STATUS_APPROVED
    assert second["reviewer_id"] == "sup-1"


@pytest.mark.unit
def test_invalid_decision_rejected(service):
    a = _request(service)
    with pytest.raises(InvalidApprovalDecision):
        service.decide(a["approval_id"], reviewer_id="sup-1", decision="maybe")


@pytest.mark.unit
def test_resume_decision_lifecycle(service):
    a = _request(service)
    assert service.get_resume_decision("run-1") is None  # 未决策
    service.decide(a["approval_id"], reviewer_id="sup-1", decision="approve")
    decision = service.get_resume_decision("run-1")
    assert decision is not None
    assert decision["decision"] == "approve"
    assert decision["approval_id"] == a["approval_id"]
    # 消费后不再返回
    service.mark_resumed(a["approval_id"])
    assert service.get_resume_decision("run-1") is None


@pytest.mark.unit
def test_list_pending_and_not_found(service):
    _request(service, action="refund")
    _request(service, action="modify_order")
    assert len(service.list_pending()) == 2
    with pytest.raises(ApprovalNotFound):
        service.require("does-not-exist")
