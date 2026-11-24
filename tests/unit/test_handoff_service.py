"""Durable human handoff: role isolation, ownership, CAS, messaging, replay."""

from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.handoff_service import (
    CLAIMED,
    CLOSED,
    OPEN,
    HandoffConflict,
    HandoffForbidden,
    HandoffNotFound,
    HandoffService,
)
from db.models import Base
from tests.unit.runtime_helpers import dispose

pytestmark = pytest.mark.unit


@pytest.fixture
def service():
    # Use distinct physical SQLite connections for simultaneous staff claims.
    # The generic test fixture uses StaticPool, which is unsuitable for
    # transactions running concurrently in different threads.
    fd, path = tempfile.mkstemp(suffix=".db", prefix="csai-handoff-")
    os.close(fd)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    with engine.connect() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
        connection.commit()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    try:
        yield HandoffService(session_factory=factory)
    finally:
        dispose(engine, path)


def _ticket(service, sid="session-a", user="customer-1"):
    return service.create_or_get(
        session_id=sid,
        customer_id=user,
        reason="客户要求转人工",
        initial_message="我需要人工客服处理订单。",
    )


def test_concurrent_create_same_session_is_one_ticket(service):
    def create(_):
        return _ticket(service)["ticket_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    assert len(set(ids)) == 1
    assert len(service.events(ids[0], actor_id="customer-1", role="customer")) == 1
    assert len(service.messages(ids[0], actor_id="customer-1", role="customer")) == 1


def test_cross_customer_session_collision_fails_closed(service):
    _ticket(service)
    with pytest.raises(HandoffForbidden):
        _ticket(service, user="customer-2")


def test_customer_can_only_read_own_ticket(service):
    tid = _ticket(service)["ticket_id"]
    with pytest.raises(HandoffNotFound):
        service.get(tid, actor_id="customer-2", role="customer")
    with pytest.raises(HandoffNotFound):
        service.messages(tid, actor_id="customer-2", role="customer")


def test_agent_queue_shows_only_open_or_assigned(service):
    first = _ticket(service, sid="session-a")["ticket_id"]
    second = _ticket(service, sid="session-b", user="customer-2")["ticket_id"]
    service.claim(first, actor_id="agent-1", role="agent")
    visible = service.queue(actor_id="agent-2", role="agent")
    assert [x["ticket_id"] for x in visible] == [second]
    own = service.queue(actor_id="agent-1", role="agent")
    assert {x["ticket_id"] for x in own} == {first, second}


def test_competing_claims_only_one_winner(service):
    tid = _ticket(service)["ticket_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(
            pool.map(
                lambda actor: _attempt_claim(service, tid, actor),
                ["agent-1", "agent-2"],
            )
        )
    assert sorted(attempts) == ["claimed", "conflict"]
    assigned = service.get(tid, actor_id="supervisor-1", role="supervisor")
    assert assigned["status"] == CLAIMED
    assert assigned["assigned_to"] in {"agent-1", "agent-2"}


def _attempt_claim(service, tid, actor):
    try:
        service.claim(tid, actor_id=actor, role="agent")
        return "claimed"
    except HandoffConflict:
        return "conflict"


def test_customer_cannot_claim(service):
    tid = _ticket(service)["ticket_id"]
    with pytest.raises(HandoffForbidden):
        service.claim(tid, actor_id="customer-1", role="customer")


def test_idempotent_message_delivery_and_staff_reply(service):
    tid = _ticket(service)["ticket_id"]
    service.claim(tid, actor_id="agent-1", role="agent")
    first = service.post_message(
        tid,
        actor_id="agent-1",
        role="agent",
        body="您好，正在跟进。",
        client_message_id="delivery-1",
    )
    second = service.post_message(
        tid,
        actor_id="agent-1",
        role="agent",
        body="您好，正在跟进。",
        client_message_id="delivery-1",
    )
    assert first["message_id"] == second["message_id"]
    assert len(service.messages(tid, actor_id="customer-1", role="customer")) == 2
    with pytest.raises(HandoffConflict):
        service.post_message(
            tid,
            actor_id="agent-1",
            role="agent",
            body="不同的内容",
            client_message_id="delivery-1",
        )


def test_unassigned_staff_cannot_post_to_claimed_ticket(service):
    tid = _ticket(service)["ticket_id"]
    service.claim(tid, actor_id="agent-1", role="agent")
    with pytest.raises(HandoffNotFound):
        service.post_message(
            tid,
            actor_id="agent-2",
            role="agent",
            body="违规发言",
            client_message_id="bad-1",
        )


def test_only_assigned_staff_can_close_and_customer_can_reopen(service):
    tid = _ticket(service)["ticket_id"]
    service.claim(tid, actor_id="agent-1", role="agent")
    with pytest.raises(HandoffForbidden):
        service.close(tid, actor_id="agent-2", role="agent", resolution="done")
    closed = service.close(tid, actor_id="agent-1", role="agent", resolution="已核查并答复")
    assert closed["status"] == CLOSED
    assert (
        service.close(tid, actor_id="agent-1", role="agent", resolution="已核查并答复")["status"]
        == CLOSED
    )
    with pytest.raises(HandoffConflict):
        service.post_message(
            tid,
            actor_id="customer-1",
            role="customer",
            body="新问题",
            client_message_id="late-1",
        )
    reopened = service.reopen(tid, actor_id="customer-1", role="customer")
    assert reopened["status"] == OPEN
    assert reopened["assigned_to"] is None
    assert [e["event"] for e in service.events(tid, actor_id="customer-1", role="customer")] == [
        "created",
        "claimed",
        "closed",
        "reopened",
    ]


def test_active_for_session_suspends_bot_only_when_open(service):
    tid = _ticket(service)["ticket_id"]
    assert (
        service.active_for_session(session_id="session-a", customer_id="customer-1")["ticket_id"]
        == tid
    )
    assert service.active_for_session(session_id="session-a", customer_id="customer-2") is None
    service.claim(tid, actor_id="agent-1", role="agent")
    service.close(tid, actor_id="agent-1", role="agent", resolution="完成")
    assert service.active_for_session(session_id="session-a", customer_id="customer-1") is None


def test_repeated_escalation_reopens_same_ticket(service):
    first = _ticket(service)
    service.claim(first["ticket_id"], actor_id="agent-1", role="agent")
    service.close(first["ticket_id"], actor_id="agent-1", role="agent", resolution="处理完毕")
    again = _ticket(service)
    assert again["ticket_id"] == first["ticket_id"]
    assert again["status"] == OPEN
    assert again["assigned_to"] is None


def test_authorized_ticket_reads_use_single_sql_visibility_snapshot(service, monkeypatch):
    tid = _ticket(service)["ticket_id"]

    def deny_out_of_band_get(*args, **kwargs):
        raise AssertionError(
            "Ticket read must not invoke a separate get() authorization transaction"
        )

    monkeypatch.setattr(service, "get", deny_out_of_band_get)
    assert len(service.messages(tid, actor_id="customer-1", role="customer")) == 1
    assert [
        event["event"]
        for event in service.events(tid, actor_id="customer-1", role="customer")
    ] == ["created"]
    with pytest.raises(HandoffNotFound):
        service.events(tid, actor_id="customer-2", role="customer")
    with pytest.raises(HandoffNotFound):
        service.messages(tid, actor_id="customer-2", role="customer")


def test_agent_loses_message_and_event_visibility_after_other_agent_claims(service):
    tid = _ticket(service)["ticket_id"]
    assert service.messages(tid, actor_id="agent-2", role="agent")
    service.claim(tid, actor_id="agent-1", role="agent")
    with pytest.raises(HandoffNotFound):
        service.messages(tid, actor_id="agent-2", role="agent")
    with pytest.raises(HandoffNotFound):
        service.events(tid, actor_id="agent-2", role="agent")
    assert service.messages(tid, actor_id="agent-1", role="agent")


def test_empty_visible_ticket_has_empty_messages_not_false_404(service):
    ticket = service.create_or_get(
        session_id="no-initial",
        customer_id="customer-1",
        reason="回访",
    )
    assert service.messages(ticket["ticket_id"], actor_id="customer-1", role="customer") == []
