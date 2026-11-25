"""Bound new industrial durable runs using the authoritative PostgreSQL state."""

from __future__ import annotations

import os

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from db.models import AgentRun
from runtime.statuses import TERMINAL_STATUSES

# Reserved application-wide transaction lock, not a worker execution lease.
_ADMISSION_LOCK = 735194022146


class RunAdmissionRejected(RuntimeError):
    def __init__(self, scope: str):
        self.scope = scope
        super().__init__("Durable run admission capacity exhausted")


class RunAdmissionUnavailable(RuntimeError):
    """The shared admission decision could not be established."""


def _limit(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= 100000:
        raise ValueError("Invalid durable admission limit")
    return value


def admit_new_run(session, *, user_id: str | None, thread_id: str, idempotency_key: str | None):
    """Serialize the check and insert in the caller's transaction.

    A matching idempotency key returns its existing row even under saturation;
    the service still verifies the original request before returning it.
    """
    if not user_id or not idempotency_key:
        raise RunAdmissionUnavailable("Industrial run requires identity and idempotency")
    try:
        global_limit = _limit("CSAI_MAX_OUTSTANDING_RUNS", 1000)
        user_limit = _limit("CSAI_MAX_OUTSTANDING_RUNS_PER_USER", 5)
        thread_limit = _limit("CSAI_MAX_OUTSTANDING_RUNS_PER_THREAD", 1)
        if session.get_bind().dialect.name != "postgresql":
            raise RunAdmissionUnavailable("Industrial run admission requires PostgreSQL")
        session.connection(execution_options={"isolation_level": "READ COMMITTED"})
        session.execute(text("SET LOCAL lock_timeout = '2s'"))
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK})
        existing = session.execute(
            select(AgentRun).where(AgentRun.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        active = AgentRun.status.notin_([status.value for status in TERMINAL_STATUSES])
        for scope, limit, predicates in (
            ("global", global_limit, (active,)),
            ("user", user_limit, (active, AgentRun.user_id == user_id)),
            ("thread", thread_limit, (active, AgentRun.thread_id == thread_id)),
        ):
            count = session.execute(select(func.count()).select_from(AgentRun).where(*predicates))
            if count.scalar_one() >= limit:
                raise RunAdmissionRejected(scope)
        return None
    except (ValueError, SQLAlchemyError) as exc:
        raise RunAdmissionUnavailable("Shared durable admission unavailable") from exc
