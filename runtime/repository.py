"""AgentRun 持久化仓库（PostgreSQL/SQLite via 现有 db 层）。

只做数据访问与原子条件更新；状态迁移规则在 ``runtime/run_service.py``。
返回普通 dict，避免 detached ORM 实例问题。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from db.database import get_db_session
from db.models import AgentDeadLetter, AgentRun

from .statuses import RunStatus, parse_status


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
        status: str = RunStatus.QUEUED.value,
    ) -> dict[str, Any]:
        now = _utcnow()
        initial = parse_status(status)
        run = AgentRun(
            id=run_id,
            thread_id=thread_id,
            session_id=session_id,
            user_id=user_id,
            status=initial.value,
            query=query,
            attempt=0,
            max_attempts=max(1, int(max_attempts)),
            created_at=now,
            queued_at=now if initial == RunStatus.QUEUED else None,
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

    def transition_owned(
        self,
        run_id: str,
        *,
        from_statuses: set[RunStatus] | frozenset[RunStatus],
        to_status: RunStatus,
        expected_worker_id: str,
        require_live_lease: bool = True,
        now: datetime | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """原子「owner 条件」迁移：status + worker_id (+ 存活 lease) 同一条 UPDATE。

        这是 worker-owned 写入的唯一正确原语。三类 predicate 都在
        ``UPDATE ... WHERE`` 内部：

        - ``id = run_id``
        - ``status IN from_statuses``
        - ``worker_id = expected_worker_id``
        - （可选但默认开启）``lease_expires_at > now``

        为什么必须在同一条 SQL 里
        ------------------------
        ``SELECT -> Python 判断 -> UPDATE`` 是 TOCTOU：SELECT 之后上下文切换、
        另一个 worker 接管，UPDATE 仍然会写进去。ownership predicate 出现在
        UPDATE 的 WHERE 里，判定与写入才是一个原子动作。

        为什么 lease 也要判
        ------------------
        ``worker_id`` 相同**不足以**证明 ownership 还在：worker 被 SIGSTOP /
        GC 停顿到 lease 过期之后，``worker_id`` 仍是它自己，但所有权已经过期。
        只比 ``worker_id`` 会让一个已过期但尚未被接管的 worker 用一次迟到的
        成功写入「证明」自己有效。lease 是 ownership 的一部分，不是装饰字段。

        刻意与 :meth:`transition` 分开而不是加可选参数：``None`` 无法同时表达
        「不检查 owner」和「要求 worker_id IS NULL」，那种二义性会让漏传参数
        静默退化成无保护写入。

        返回更新后的 run；条件不满足返回 ``None``。
        """
        allowed = [s.value for s in from_statuses]
        values: dict[str, Any] = {"status": to_status.value, "updated_at": _utcnow()}
        values.update(fields)
        predicates = [
            AgentRun.id == run_id,
            AgentRun.status.in_(allowed),
            AgentRun.worker_id == expected_worker_id,
        ]
        if require_live_lease:
            predicates.append(
                AgentRun.lease_expires_at.isnot(None)
                & (AgentRun.lease_expires_at > (now or _utcnow()))
            )
        session = self._session()
        try:
            result = session.execute(update(AgentRun).where(*predicates).values(**values))
            session.commit()
            if result.rowcount == 0:
                return None
        finally:
            session.close()
        return self.get(run_id)

    def renew_lease_owned(
        self,
        run_id: str,
        *,
        expected_worker_id: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        """原子续租：``id + status=RUNNING + worker_id + lease 未过期`` 同一条 UPDATE。

        过期 owner 不能靠「晚到的一次续租」给自己续命——严格 lease expiry 语义。
        返回是否真的续上了（rowcount == 1）。
        """
        now = now or _utcnow()
        session = self._session()
        try:
            result = session.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.status == RunStatus.RUNNING.value,
                    AgentRun.worker_id == expected_worker_id,
                    AgentRun.lease_expires_at.isnot(None),
                    AgentRun.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    heartbeat_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

    def takeover_running(
        self,
        run_id: str,
        *,
        worker_id: str,
        task_id: str | None,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """原子 RUNNING takeover：只有「当前 owner 已失效」时才接管。

        谓词：``status = RUNNING AND (worker_id = :new OR worker_id IS NULL
        OR lease_expires_at IS NULL OR lease_expires_at <= :now)``。

        这一点很关键：旧实现先 SELECT 看到「lease 过期」，再用一个只判
        ``status = RUNNING`` 的 UPDATE 写入。两个竞争者 B 和 C 可以读到同一份
        「A 已过期」的快照，然后**都**成功接管。谓词放进同一条 UPDATE 后，
        B 成功写入即把 ``worker_id`` 改成 B、lease 变成未来时刻，于是 C 的谓词
        立刻不再成立，只有一个人能接管。``attempt`` 在 SQL 内 ``+ 1``，不依赖
        之前的快照。

        返回更新后的 run；未取得所有权返回 ``None``。
        """
        now = now or _utcnow()
        session = self._session()
        try:
            result = session.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.status == RunStatus.RUNNING.value,
                    or_(
                        AgentRun.worker_id == worker_id,
                        AgentRun.worker_id.is_(None),
                        AgentRun.lease_expires_at.is_(None),
                        AgentRun.lease_expires_at <= now,
                    ),
                )
                .values(
                    status=RunStatus.RUNNING.value,
                    worker_id=worker_id,
                    task_id=task_id,
                    attempt=AgentRun.attempt + 1,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    heartbeat_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            if result.rowcount == 0:
                return None
        finally:
            session.close()
        return self.get(run_id)

    def touch_queued(self, run_id: str, when: datetime) -> None:
        """刷新 queued_at（重放重新投递时使用）。"""
        session = self._session()
        try:
            session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(queued_at=when, updated_at=_utcnow())
            )
            session.commit()
        finally:
            session.close()

    def list_recoverable_runs(
        self, *, limit: int = 100, now=None, orphan_grace_seconds: int = 60
    ) -> list[str]:
        """RETRYING/QUEUED 且已到重投时刻的 run（等待重投但无人调度）。

        两类都要捞：

        1. ``next_retry_at`` 非空且已过期 —— 退避已到、但重投消息没发出去/丢了。
        2. ``next_retry_at`` 为 **NULL** 的 QUEUED run —— 正常入队的 run 就是
           ``next_retry_at = NULL``。这正是"已落库但消息丢失"（broker 丢消息）
           的形态；只匹配非空会把它整类漏掉，run 永远停在 QUEUED。

        NULL 那一类必须带年龄下限（``orphan_grace_seconds``）：刚入队的 QUEUED run
        消息可能正在投递途中，立刻重投会造成不必要的重复执行。
        """
        from datetime import datetime, timedelta, timezone

        now = now or datetime.now(timezone.utc)
        grace_cutoff = now - timedelta(seconds=max(0, int(orphan_grace_seconds)))
        stmt = (
            select(AgentRun.id)
            .where(
                AgentRun.status.in_([RunStatus.RETRYING.value, RunStatus.QUEUED.value]),
                or_(
                    and_(
                        AgentRun.next_retry_at.isnot(None),
                        AgentRun.next_retry_at <= now,
                    ),
                    and_(
                        AgentRun.next_retry_at.is_(None),
                        AgentRun.status == RunStatus.QUEUED.value,
                        AgentRun.created_at <= grace_cutoff,
                    ),
                ),
            )
            .order_by(AgentRun.created_at.asc())
            .limit(max(1, int(limit)))
        )
        session = self._session()
        try:
            return [row[0] for row in session.execute(stmt).all()]
        finally:
            session.close()

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
