"""AgentRun 持久化仓库（PostgreSQL/SQLite via 现有 db 层）。

只做数据访问与原子条件更新；状态迁移规则在 ``runtime/run_service.py``。
返回普通 dict，避免 detached ORM 实例问题。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from db.database import get_db_session
from db.models import AgentDeadLetter, AgentRun

from .statuses import RunStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_dict(run: AgentRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "thread_id": run.thread_id,
        "session_id": run.session_id,
        "user_id": run.user_id,
        "status": run.status,
        "query": run.query,
        "result": run.result,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "error_type": run.error_type,
        "last_error": run.last_error,
        "attempt": run.attempt,
        "max_attempts": run.max_attempts,
        "created_at": run.created_at,
        "queued_at": run.queued_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "updated_at": run.updated_at,
        "trace_id": run.trace_id,
        "idempotency_key": run.idempotency_key,
        "worker_id": run.worker_id,
        "task_id": run.task_id,
        "lease_expires_at": run.lease_expires_at,
        "heartbeat_at": run.heartbeat_at,
        "next_retry_at": run.next_retry_at,
    }


def dead_letter_to_dict(row: AgentDeadLetter) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "thread_id": row.thread_id,
        "attempt_count": row.attempt_count,
        "max_attempts": row.max_attempts,
        "error_type": row.error_type,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "worker_id": row.worker_id,
        "entered_at": row.entered_at,
        "created_at": row.created_at,
    }


class AgentRunRepository:
    """基于注入 session factory 的仓库，便于测试使用独立 SQLite/PostgreSQL。"""

    def __init__(self, session_factory: Callable[[], Any] | None = None):
        self._session_factory = session_factory or get_db_session

    def _session(self):
        return self._session_factory()

    def create(
        self,
        *,
        run_id: str,
        thread_id: str,
        session_id: str,
        query: str,
        user_id: str | None = None,
        max_attempts: int = 3,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        now = _utcnow()
        run = AgentRun(
            id=run_id,
            thread_id=thread_id,
            session_id=session_id,
            user_id=user_id,
            status=RunStatus.QUEUED.value,
            query=query,
            attempt=0,
            max_attempts=max(1, int(max_attempts)),
            created_at=now,
            queued_at=now,
            updated_at=now,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
        )
        session = self._session()
        try:
            session.add(run)
            session.commit()
            session.refresh(run)
            return to_dict(run)
        except IntegrityError:
            session.rollback()
            if idempotency_key:
                existing = self.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return existing
            raise
        finally:
            session.close()

    def get(self, run_id: str) -> dict[str, Any] | None:
        session = self._session()
        try:
            run = session.get(AgentRun, run_id)
            return to_dict(run) if run is not None else None
        finally:
            session.close()

    def get_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        session = self._session()
        try:
            run = session.execute(
                select(AgentRun).where(AgentRun.idempotency_key == key)
            ).scalar_one_or_none()
            return to_dict(run) if run is not None else None
        finally:
            session.close()

    def list_by_status(self, status: RunStatus | str, limit: int = 100) -> list[dict[str, Any]]:
        value = status.value if isinstance(status, RunStatus) else str(status)
        session = self._session()
        try:
            runs = (
                session.execute(
                    select(AgentRun)
                    .where(AgentRun.status == value)
                    .order_by(AgentRun.created_at.asc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [to_dict(r) for r in runs]
        finally:
            session.close()

    def list_by_statuses(
        self, statuses: list[RunStatus | str], limit: int = 100
    ) -> list[dict[str, Any]]:
        values = [s.value if isinstance(s, RunStatus) else str(s) for s in statuses]
        session = self._session()
        try:
            runs = (
                session.execute(
                    select(AgentRun)
                    .where(AgentRun.status.in_(values))
                    .order_by(AgentRun.created_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [to_dict(r) for r in runs]
        finally:
            session.close()

    def transition(
        self,
        run_id: str,
        *,
        from_statuses: set[RunStatus] | frozenset[RunStatus],
        to_status: RunStatus,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """原子条件迁移：仅当当前状态在 from_statuses 中才更新。

        返回更新后的 run；条件不满足（并发变化）返回 None。
        """
        allowed = [s.value for s in from_statuses]
        values: dict[str, Any] = {"status": to_status.value, "updated_at": _utcnow()}
        values.update(fields)
        session = self._session()
        try:
            result = session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id, AgentRun.status.in_(allowed))
                .values(**values)
            )
            session.commit()
            if result.rowcount == 0:
                return None
        finally:
            session.close()
        return self.get(run_id)

    # ---- Dead letter ----

    def add_dead_letter(
        self,
        *,
        run_id: str,
        thread_id: str,
        attempt_count: int,
        max_attempts: int,
        error_type: str | None,
        error_code: str | None,
        error_message: str | None,
        worker_id: str | None,
    ) -> dict[str, Any]:
        now = _utcnow()
        row = AgentDeadLetter(
            run_id=run_id,
            thread_id=thread_id,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            error_type=error_type,
            error_code=error_code,
            error_message=(error_message or "")[:2000] or None,
            worker_id=worker_id,
            entered_at=now,
            created_at=now,
        )
        session = self._session()
        try:
            existing = session.execute(
                select(AgentDeadLetter).where(AgentDeadLetter.run_id == run_id)
            ).scalar_one_or_none()
            if existing is not None:
                return dead_letter_to_dict(existing)
            session.add(row)
            session.commit()
            session.refresh(row)
            return dead_letter_to_dict(row)
        except IntegrityError:
            session.rollback()
            existing = session.execute(
                select(AgentDeadLetter).where(AgentDeadLetter.run_id == run_id)
            ).scalar_one_or_none()
            if existing is not None:
                return dead_letter_to_dict(existing)
            raise
        finally:
            session.close()

    def get_dead_letter(self, run_id: str) -> dict[str, Any] | None:
        session = self._session()
        try:
            row = session.execute(
                select(AgentDeadLetter).where(AgentDeadLetter.run_id == run_id)
            ).scalar_one_or_none()
            return dead_letter_to_dict(row) if row is not None else None
        finally:
            session.close()

    def list_dead_letters(self, limit: int = 100) -> list[dict[str, Any]]:
        session = self._session()
        try:
            rows = (
                session.execute(
                    select(AgentDeadLetter).order_by(AgentDeadLetter.entered_at.desc()).limit(limit)
                )
                .scalars()
                .all()
            )
            return [dead_letter_to_dict(r) for r in rows]
        finally:
            session.close()
