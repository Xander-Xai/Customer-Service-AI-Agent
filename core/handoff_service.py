"""Durable customer-service handoff lifecycle.

Human support transfer is NOT the HITL approval gate for side-effect tools.
All transitions are database conditional updates, not process-local locks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import or_, select, true, update
from sqlalchemy.exc import IntegrityError

from db.models import HandoffEvent, HandoffMessage, HandoffTicket

OPEN = "OPEN"
CLAIMED = "CLAIMED"
CLOSED = "CLOSED"
STAFF_ROLES = frozenset({"agent", "supervisor", "admin"})
MANAGER_ROLES = frozenset({"supervisor", "admin"})


class HandoffNotFound(LookupError):
    pass


class HandoffForbidden(PermissionError):
    pass


class HandoffConflict(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ticket(row: HandoffTicket) -> dict:
    return {
        "ticket_id": row.id,
        "session_id": row.session_id,
        "customer_id": row.customer_id,
        "status": row.status,
        "assigned_to": row.assigned_to,
        "reason": row.reason,
        "source": row.source,
        "resolution": row.resolution,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "claimed_at": row.claimed_at,
        "closed_at": row.closed_at,
    }


def _message(row: HandoffMessage) -> dict:
    return {
        "message_id": row.id,
        "ticket_id": row.ticket_id,
        "author_id": row.author_id,
        "sender_role": row.sender_role,
        "body": row.body,
        "created_at": row.created_at,
    }


class HandoffService:
    def __init__(self, session_factory=None):
        if session_factory is None:
            from db.database import SessionLocal

            session_factory = SessionLocal
        self._session_factory = session_factory

    @staticmethod
    def _text(value: str, *, maximum: int = 4096) -> str:
        value = str(value or "").strip()
        if not value or len(value) > maximum:
            raise ValueError("message must be nonempty and within the length limit")
        return value

    @staticmethod
    def _actor(actor_id: str, role: str) -> tuple[str, str]:
        actor = str(actor_id or "").strip()
        role = str(role or "").lower()
        if not actor:
            raise HandoffForbidden("Authenticated actor required")
        if role not in {"customer", "agent", "supervisor", "admin"}:
            raise HandoffForbidden("Unsupported role")
        return actor, role

    @staticmethod
    def _event(db, row: HandoffTicket, actor_id: str, name: str) -> None:
        db.add(
            HandoffEvent(
                id=str(uuid4()), ticket_id=row.id, actor_id=actor_id, event=name, created_at=_now()
            )
        )

    @staticmethod
    def _visibility_predicate(actor: str, role: str):
        """Authorization checked by the *same SQL statement* that returns messages."""
        if role in MANAGER_ROLES:
            return true()
        if role == "agent":
            return or_(
                HandoffTicket.customer_id == actor,
                HandoffTicket.status == OPEN,
                HandoffTicket.assigned_to == actor,
            )
        return HandoffTicket.customer_id == actor

    @staticmethod
    def _visible(row: HandoffTicket, actor: str, role: str) -> bool:
        return (
            row.customer_id == actor
            or role in MANAGER_ROLES
            or (role == "agent" and (row.status == OPEN or row.assigned_to == actor))
        )

    def create_or_get(
        self,
        *,
        session_id: str,
        customer_id: str,
        reason: str,
        source: str = "customer",
        initial_message: str | None = None,
    ) -> dict:
        """One persistent ticket per session; a closed ticket may be reopened.

        Unique session_id resolves races between concurrent escalations.
        Initial customer text is stored once on first creation.
        """
        session_id = self._text(session_id, maximum=64)
        customer_id = self._text(customer_id, maximum=64)
        reason = self._text(reason, maximum=2000)
        if source not in {"customer", "agent"}:
            raise ValueError("Unsupported handoff source")
        with self._session_factory() as db:
            row = db.scalar(select(HandoffTicket).where(HandoffTicket.session_id == session_id))
            if row is not None and row.customer_id != customer_id:
                raise HandoffForbidden("Session belongs to another customer")
            if row is None:
                row = HandoffTicket(
                    id=str(uuid4()),
                    session_id=session_id,
                    customer_id=customer_id,
                    reason=reason,
                    source=source,
                    status=OPEN,
                    created_at=_now(),
                    updated_at=_now(),
                )
                db.add(row)
                try:
                    # Flush the parent before inserting child audit/message rows.
                    # An existing concurrent creator is resolved by the DB's
                    # unique session index, never a process-local mutex.
                    db.flush()
                    self._event(db, row, customer_id, "created")
                    if initial_message:
                        db.add(
                            HandoffMessage(
                                id=str(uuid4()),
                                ticket_id=row.id,
                                author_id=customer_id,
                                sender_role="customer",
                                client_message_id="initial",
                                body=self._text(initial_message),
                                created_at=_now(),
                            )
                        )
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    row = db.scalar(
                        select(HandoffTicket).where(HandoffTicket.session_id == session_id)
                    )
                    if row is None:
                        raise
                    if row.customer_id != customer_id:
                        raise HandoffForbidden("Session belongs to another customer") from None
            elif row.status == CLOSED:
                changed = db.execute(
                    update(HandoffTicket)
                    .where(HandoffTicket.id == row.id, HandoffTicket.status == CLOSED)
                    .values(
                        status=OPEN,
                        assigned_to=None,
                        claimed_at=None,
                        closed_at=None,
                        resolution=None,
                        reason=reason,
                        updated_at=_now(),
                    )
                ).rowcount
                if changed:
                    self._event(db, row, customer_id, "reopened")
                    if initial_message:
                        # A new escalation after closure must deliver the new
                        # customer request, rather than silently reusing the
                        # original ticket with its obsolete first message.
                        db.add(
                            HandoffMessage(
                                id=str(uuid4()),
                                ticket_id=row.id,
                                author_id=customer_id,
                                sender_role="customer",
                                client_message_id=f"reopen:{uuid4().hex}",
                                body=self._text(initial_message),
                                created_at=_now(),
                            )
                        )
                db.commit()
            db.refresh(row)
            return _ticket(row)

    def active_for_session(self, *, session_id: str, customer_id: str) -> dict | None:
        with self._session_factory() as db:
            row = db.scalar(
                select(HandoffTicket).where(
                    HandoffTicket.session_id == session_id,
                    HandoffTicket.customer_id == customer_id,
                    HandoffTicket.status.in_((OPEN, CLAIMED)),
                )
            )
            return _ticket(row) if row else None

    def get(self, ticket_id: str, *, actor_id: str, role: str) -> dict:
        actor, role = self._actor(actor_id, role)
        with self._session_factory() as db:
            row = db.get(HandoffTicket, ticket_id)
            if row is None or not self._visible(row, actor, role):
                raise HandoffNotFound("Ticket not found")
            return _ticket(row)

    def by_session(self, session_id: str, *, actor_id: str, role: str) -> dict | None:
        actor, role = self._actor(actor_id, role)
        with self._session_factory() as db:
            row = db.scalar(select(HandoffTicket).where(HandoffTicket.session_id == session_id))
            if row is None:
                return None
            if not self._visible(row, actor, role):
                raise HandoffNotFound("Ticket not found")
            return _ticket(row)

    def queue(
        self, *, actor_id: str, role: str, status: str | None = None, limit: int = 50
    ) -> list[dict]:
        actor, role = self._actor(actor_id, role)
        if role not in STAFF_ROLES:
            raise HandoffForbidden("Staff role required")
        if status is not None and status not in {OPEN, CLAIMED, CLOSED}:
            raise ValueError("Invalid status")
        with self._session_factory() as db:
            q = select(HandoffTicket)
            if role == "agent":
                q = q.where(or_(HandoffTicket.status == OPEN, HandoffTicket.assigned_to == actor))
            if status:
                q = q.where(HandoffTicket.status == status)
            return [
                _ticket(row)
                for row in db.scalars(
                    q.order_by(HandoffTicket.created_at.asc()).limit(min(max(limit, 1), 100))
                ).all()
            ]

    def claim(self, ticket_id: str, *, actor_id: str, role: str) -> dict:
        actor, role = self._actor(actor_id, role)
        if role not in STAFF_ROLES:
            raise HandoffForbidden("Staff role required")
        with self._session_factory() as db:
            changed = db.execute(
                update(HandoffTicket)
                .where(
                    HandoffTicket.id == ticket_id,
                    HandoffTicket.status == OPEN,
                    HandoffTicket.assigned_to.is_(None),
                )
                .values(status=CLAIMED, assigned_to=actor, claimed_at=_now(), updated_at=_now())
            ).rowcount
            row = db.get(HandoffTicket, ticket_id)
            if row is None:
                raise HandoffNotFound("Ticket not found")
            if changed:
                self._event(db, row, actor, "claimed")
                db.commit()
                db.refresh(row)
            elif row.status != CLAIMED or row.assigned_to != actor:
                raise HandoffConflict("Ticket already claimed or closed")
            return _ticket(row)

    def post_message(
        self,
        ticket_id: str,
        *,
        actor_id: str,
        role: str,
        body: str,
        client_message_id: str,
    ) -> dict:
        actor, role = self._actor(actor_id, role)
        body = self._text(body)
        key = self._text(client_message_id, maximum=64)
        with self._session_factory() as db:
            row = db.scalar(
                select(HandoffTicket)
                .where(HandoffTicket.id == ticket_id)
                .with_for_update()
            )
            if row is None:
                raise HandoffNotFound("Ticket not found")
            customer = row.customer_id == actor
            staff = role in MANAGER_ROLES or (role == "agent" and row.assigned_to == actor)
            if not customer and not staff:
                raise HandoffNotFound("Ticket not found")
            existing = db.scalar(
                select(HandoffMessage).where(
                    HandoffMessage.ticket_id == ticket_id,
                    HandoffMessage.author_id == actor,
                    HandoffMessage.client_message_id == key,
                )
            )
            if existing is not None:
                if existing.body != body:
                    raise HandoffConflict("Idempotency key already used with different content")
                return _message(existing)
            if row.status == CLOSED:
                raise HandoffConflict("Ticket is closed")
            message = HandoffMessage(
                id=str(uuid4()),
                ticket_id=row.id,
                author_id=actor,
                sender_role="customer" if customer else "agent",
                client_message_id=key,
                body=body,
                created_at=_now(),
            )
            db.add(message)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.scalar(
                    select(HandoffMessage).where(
                        HandoffMessage.ticket_id == ticket_id,
                        HandoffMessage.author_id == actor,
                        HandoffMessage.client_message_id == key,
                    )
                )
                if existing is None:
                    raise
                if existing.body != body:
                    raise HandoffConflict(
                        "Idempotency key already used with different content"
                    ) from None
                return _message(existing)
            db.refresh(message)
            return _message(message)

    def messages(self, ticket_id: str, *, actor_id: str, role: str, limit: int = 100) -> list[dict]:
        actor, role = self._actor(actor_id, role)
        with self._session_factory() as db:
            # One database snapshot checks the caller's current visibility
            # and returns messages; no independent authorization/read sessions.
            result = db.execute(
                select(HandoffTicket.id, HandoffMessage)
                .outerjoin(HandoffMessage, HandoffMessage.ticket_id == HandoffTicket.id)
                .where(
                    HandoffTicket.id == ticket_id,
                    self._visibility_predicate(actor, role),
                )
                .order_by(HandoffMessage.created_at.asc(), HandoffMessage.id.asc())
                .limit(min(max(limit, 1), 200))
            ).all()
            if not result:
                raise HandoffNotFound("Ticket not found")
            return [_message(message) for _, message in result if message is not None]

    def close(self, ticket_id: str, *, actor_id: str, role: str, resolution: str) -> dict:
        actor, role = self._actor(actor_id, role)
        resolution = self._text(resolution, maximum=2000)
        with self._session_factory() as db:
            row = db.get(HandoffTicket, ticket_id)
            if row is None:
                raise HandoffNotFound("Ticket not found")
            if role not in MANAGER_ROLES and (role != "agent" or row.assigned_to != actor):
                raise HandoffForbidden("Assigned staff or supervisor required")
            if row.status == CLOSED:
                if row.resolution == resolution:
                    return _ticket(row)
                raise HandoffConflict("Ticket already closed with another resolution")
            changed = db.execute(
                update(HandoffTicket)
                .where(HandoffTicket.id == ticket_id, HandoffTicket.status == CLAIMED)
                .values(status=CLOSED, resolution=resolution, closed_at=_now(), updated_at=_now())
            ).rowcount
            if changed != 1:
                raise HandoffConflict("Ticket must be claimed before closing")
            self._event(db, row, actor, "closed")
            db.commit()
            db.refresh(row)
            return _ticket(row)

    def reopen(self, ticket_id: str, *, actor_id: str, role: str) -> dict:
        actor, role = self._actor(actor_id, role)
        with self._session_factory() as db:
            row = db.get(HandoffTicket, ticket_id)
            if row is None or not (row.customer_id == actor or role in MANAGER_ROLES):
                raise HandoffNotFound("Ticket not found")
            if row.status != CLOSED:
                return _ticket(row)
            changed = db.execute(
                update(HandoffTicket)
                .where(HandoffTicket.id == ticket_id, HandoffTicket.status == CLOSED)
                .values(
                    status=OPEN,
                    assigned_to=None,
                    claimed_at=None,
                    closed_at=None,
                    resolution=None,
                    updated_at=_now(),
                )
            ).rowcount
            if changed:
                self._event(db, row, actor, "reopened")
            db.commit()
            db.refresh(row)
            return _ticket(row)

    def events(self, ticket_id: str, *, actor_id: str, role: str) -> list[dict]:
        actor, role = self._actor(actor_id, role)
        with self._session_factory() as db:
            result = db.execute(
                select(HandoffTicket.id, HandoffEvent)
                .outerjoin(HandoffEvent, HandoffEvent.ticket_id == HandoffTicket.id)
                .where(
                    HandoffTicket.id == ticket_id,
                    self._visibility_predicate(actor, role),
                )
                .order_by(HandoffEvent.created_at.asc(), HandoffEvent.id.asc())
                .limit(1000)
            ).all()
            if not result:
                raise HandoffNotFound("Ticket not found")
            return [
                {
                    "actor_id": event.actor_id,
                    "event": event.event,
                    "created_at": event.created_at,
                }
                for _, event in result
                if event is not None
            ]


_default_service: HandoffService | None = None


def get_handoff_service() -> HandoffService:
    global _default_service
    if _default_service is None:
        _default_service = HandoffService()
    return _default_service
