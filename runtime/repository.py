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

from .statuses import TERMINAL_STATUSES, RunStatus, parse_status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: 「可能丢消息」的两种状态的共同宽限期（秒）。
#:
#: ``QUEUED``（``next_retry_at IS NULL``，即单阶段入队 publish 结果未知）与
#: ``PENDING``（两阶段入队，确认未提交）是**同一类不确定性**：消息可能已发出，也可能
#: 没发出，数据库这边无法区分。两种状态用**同一个**宽限期，才能保证两者的实际检测
#: 延迟一致（= 本值 + 一次扫描间隔），也避免两个 magic number 悄悄漂移。
#:
#: 实测延迟关系见 ``docs/reference/runtime-state-convergence.md`` 第 4 节：扫描间隔
#: （``AGENT_RUN_RECONCILE_INTERVAL_SECONDS``，默认 30s）**不是**恢复时间。
ORPHAN_GRACE_SECONDS = 60


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

    # ---- 原子「收敛到 DEAD_LETTER」原语 ----

    @staticmethod
    def _attach_dead_letter(
        session: Any,
        row: AgentRun,
        *,
        error_code: str | None,
        error_message: str | None,
        error_type: str | None,
        now: datetime,
    ) -> None:
        """在**同一个事务**里写 ``agent_dead_letters`` 行。

        调用方已经通过条件 UPDATE 独占了这行 run（``status`` 随之变成终态，
        第二个竞争者的 ``rowcount`` 会是 0），所以这里先查后插不会与并发写者
        打架；``agent_dead_letters.run_id`` 的唯一约束是兜底。

        既有行**不可变**：重复补写返回原行，保留首次进入 DLQ 时的
        ``attempt_count`` / ``error_code`` / ``entered_at``（重放后再次进入 DLQ
        不能覆盖历史证据，见 ``RunService.requeue_dead_letter``）。
        """
        existing = session.execute(
            select(AgentDeadLetter).where(AgentDeadLetter.run_id == row.id)
        ).scalar_one_or_none()
        if existing is not None:
            return
        session.add(
            AgentDeadLetter(
                run_id=row.id,
                thread_id=row.thread_id,
                attempt_count=int(row.attempt or 0),
                max_attempts=int(row.max_attempts or 1),
                error_type=error_type,
                error_code=error_code,
                error_message=(error_message or "")[:2000] or None,
                worker_id=row.worker_id,
                entered_at=now,
                created_at=now,
            )
        )

    def _dead_letter_conditionally(
        self,
        *,
        run_id: str,
        predicates: list[Any],
        error_code: str,
        error_message: str,
        error_type: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        """条件 UPDATE -> DEAD_LETTER，并在**同一事务**写 DLQ 行。

        为什么必须同一事务
        ------------------
        状态迁移与 DLQ 记录分开两次提交会留下一个**不可恢复**的窗口：状态已是终态
        （不再被任何扫描器捞起）而 DLQ 行缺失，于是这个 run 既不会重投、也没有
        ``agent_dead_letters`` 记录供人工 ``scripts/replay_dead_run.py`` 重放——
        它变成静默消失。同事务让两者同生共死。

        返回 ``None`` = 谓词不成立（已被接走 / 已续租 / 已终态），调用方必须当成
        benign no-op。
        """
        values = {
            "status": RunStatus.DEAD_LETTER.value,
            "error_code": error_code,
            "error_message": (error_message or "")[:2000] or None,
            "error_type": error_type,
            "last_error": (error_message or "")[:2000] or None,
            "lease_expires_at": None,
            "finished_at": now,
            "updated_at": now,
        }
        session = self._session()
        try:
            row = session.execute(
                update(AgentRun).where(*predicates).values(**values).returning(AgentRun)
            ).scalar_one_or_none()
            if row is None:
                session.rollback()
                return None
            # commit 之前先把快照取出来：commit 之后 session 可能 expire 该实例
            snapshot = to_dict(row)
            self._attach_dead_letter(
                session,
                row,
                error_code=error_code,
                error_message=error_message,
                error_type=error_type,
                now=now,
            )
            session.commit()
            return snapshot
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

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
        """原子 RUNNING takeover：只有「当前 owner 已失效」**且**预算未耗尽时才接管。

        谓词：``status = RUNNING AND attempt < max_attempts AND
        (worker_id = :new OR worker_id IS NULL OR lease_expires_at IS NULL
        OR lease_expires_at <= :now)``。

        这一点很关键：旧实现先 SELECT 看到「lease 过期」，再用一个只判
        ``status = RUNNING`` 的 UPDATE 写入。两个竞争者 B 和 C 可以读到同一份
        「A 已过期」的快照，然后**都**成功接管。谓词放进同一条 UPDATE 后，
        B 成功写入即把 ``worker_id`` 改成 B、lease 变成未来时刻，于是 C 的谓词
        立刻不再成立，只有一个人能接管。``attempt`` 在 SQL 内 ``+ 1``，不依赖
        之前的快照。

        预算谓词为什么必须在这条 UPDATE 里（而不是服务层先判）
        ------------------------------------------------------
        服务层「先 SELECT 判预算、再 UPDATE」留下的窗口正是本次修复的缺陷：
        ``attempt == max_attempts`` 是**最后一次正常执行**的常态，此时一条只判
        ``status = RUNNING`` 的预算耗尽写入会把持有有效 lease、正在跑图的 run
        打成 ``DEAD_LETTER``。把 ``attempt < max_attempts`` 放进同一条 UPDATE 后，
        「能否接管」与「预算是否允许」由数据库一次性判定：预算耗尽时这里必然
        返回 ``None``，调用方再走 lease 谓词门控的 DLQ 收敛（见
        :meth:`dead_letter_stale_running`），那条路径要求 lease 已过期。

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
                    AgentRun.attempt < AgentRun.max_attempts,
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

    def list_stale_running_runs(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """RUNNING 且 **lease 已过期** 的 run：owner 已被证明失效。

        为什么必须单独扫这一类
        ----------------------
        :meth:`list_recoverable_runs` 只覆盖 ``RETRYING`` / ``QUEUED``。一旦一个
        run 停在 ``RUNNING``（worker 执行途中 lease 过期、完成提交被 owner CAS
        拒绝、任务却已正常返回被 ACK），没有任何扫描器会看到它：broker 不会再投递，
        reconciler 也不会捞它，run 就**永久**停在 RUNNING——没有终态、没有 DLQ、
        没有告警。这正是 ``test_worker_checkpoint_recovery`` 偶发失败的形态。

        判定条件只用 ``lease_expires_at <= now``，与 :meth:`takeover_running` 信任的
        是**同一个**信号（owner 的 lease 已过期 = owner 已被证明失效），因此这里
        没有引入新的信任边界。仍然持有有效 lease 的 run（心跳正常）不会被选中。

        返回 ``id`` / ``attempt`` / ``max_attempts``：调用方据此区分「还可以再执行
        一次」与「预算已耗尽，应落 DLQ」，避免无界重投。
        """
        now = now or _utcnow()
        stmt = (
            select(
                AgentRun.id,
                AgentRun.attempt,
                AgentRun.max_attempts,
                AgentRun.lease_expires_at,
            )
            .where(
                AgentRun.status == RunStatus.RUNNING.value,
                AgentRun.lease_expires_at.isnot(None),
                AgentRun.lease_expires_at <= now,
            )
            .order_by(AgentRun.lease_expires_at.asc())
            .limit(max(1, int(limit)))
        )
        session = self._session()
        try:
            return [
                {
                    "id": r[0],
                    "attempt": int(r[1] or 0),
                    "max_attempts": int(r[2] or 1),
                    "lease_expires_at": r[3],
                }
                for r in session.execute(stmt).all()
            ]
        finally:
            session.close()

    def transition_stale_running(
        self,
        run_id: str,
        *,
        to_status: RunStatus,
        now: datetime | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """原子「仍然过期」的 RUNNING 迁移（reconciler 专用）。

        谓词 ``status=RUNNING AND lease_expires_at IS NOT NULL AND
        lease_expires_at <= now`` 与 :meth:`list_stale_running_runs` 一致。

        带上「仍然过期」这一步是必须的：reconciler 扫描与写入之间可能隔了任意长
        时间，其间一个活着的 worker 可能刚刚续上 lease。只按 ``status=RUNNING``
        更新就会把一个正在正常执行的 run 误改终态。续租一旦成功，谓词立刻不成立，
        这次写入被挡下——与 ``takeover_running`` 是同一种「判定与写入原子化」。

        ``require_attempt_exhausted=True`` 追加 ``attempt >= max_attempts``。
        该谓词是**列对列**比较，不依赖任何 Python 快照，因此
        「扫描时看到预算耗尽」与「写入时仍预算耗尽」之间没有 TOCTOU。
        """
        now = now or _utcnow()
        predicates: list[Any] = [
            AgentRun.id == run_id,
            AgentRun.status == RunStatus.RUNNING.value,
            AgentRun.lease_expires_at.isnot(None),
            AgentRun.lease_expires_at <= now,
        ]
        if fields.pop("require_attempt_exhausted", False):
            predicates.append(AgentRun.attempt >= AgentRun.max_attempts)
        values: dict[str, Any] = {"status": to_status.value, "updated_at": now}
        values.update(fields)
        session = self._session()
        try:
            result = session.execute(update(AgentRun).where(*predicates).values(**values))
            session.commit()
            if result.rowcount == 0:
                return None
        finally:
            session.close()
        return self.get(run_id)

    def dead_letter_stale_running(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
        error_code: str,
        error_message: str,
        error_type: str = "transient",
    ) -> dict[str, Any] | None:
        """原子收敛「lease 已过期 + 预算已耗尽」的 RUNNING 到 DEAD_LETTER。

        全部判定在**同一条 UPDATE** 的 WHERE 里：

        - ``id = run_id``
        - ``status = RUNNING``
        - ``lease_expires_at IS NOT NULL AND lease_expires_at <= now``（owner 已被
          证明失效；**仍持有有效 lease 的 run 绝不会被改写**）
        - ``attempt >= max_attempts``（预算耗尽，不再执行）

        DLQ 行在同事务写入（见 :meth:`_dead_letter_conditionally`）。

        返回 ``None`` 表示谓词不成立：run 已被接管 / 刚续上租 / 已终态 / 预算未
        耗尽。调用方必须把它当 benign no-op。
        """
        now = now or _utcnow()
        return self._dead_letter_conditionally(
            run_id=run_id,
            predicates=[
                AgentRun.id == run_id,
                AgentRun.status == RunStatus.RUNNING.value,
                AgentRun.lease_expires_at.isnot(None),
                AgentRun.lease_expires_at <= now,
                AgentRun.attempt >= AgentRun.max_attempts,
            ],
            error_code=error_code,
            error_message=error_message,
            error_type=error_type,
            now=now,
        )

    def claim_executable(
        self,
        run_id: str,
        *,
        from_statuses: set[RunStatus] | frozenset[RunStatus],
        worker_id: str | None,
        task_id: str | None,
        lease_seconds: float | None,
        set_started_at: bool,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """原子 QUEUED/RETRYING -> RUNNING 领取（含预算谓词 + SQL 内 attempt+1）。

        三个判定在同一条 UPDATE 内：``status IN (...)``、``attempt < max_attempts``、
        写入 ``attempt = attempt + 1``。因此

        - 并发重复投递里**恰好一个** worker 拿到执行权（``rowcount == 1``）；
        - 落败者的投递**不消耗预算**（attempt 只在获胜的 UPDATE 里 +1）；
        - 预算耗尽时这里必然返回 ``None``，由调用方走
          :meth:`dead_letter_exhausted_executable` 原子收敛。

        返回 ``None`` = 未取得执行权（并发竞争输了 / 预算耗尽 / 状态已变）。
        """
        now = now or _utcnow()
        values: dict[str, Any] = {
            "status": RunStatus.RUNNING.value,
            "attempt": AgentRun.attempt + 1,
            "worker_id": worker_id,
            "task_id": task_id,
            "updated_at": now,
        }
        if set_started_at:
            values["started_at"] = now
        if lease_seconds is not None:
            values["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
            values["heartbeat_at"] = now
        allowed = [s.value for s in from_statuses]
        session = self._session()
        try:
            result = session.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.status.in_(allowed),
                    AgentRun.attempt < AgentRun.max_attempts,
                )
                .values(**values)
            )
            session.commit()
            if result.rowcount == 0:
                return None
        finally:
            session.close()
        return self.get(run_id)

    def dead_letter_exhausted_executable(
        self,
        run_id: str,
        *,
        from_statuses: set[RunStatus] | frozenset[RunStatus],
        now: datetime | None = None,
        error_code: str,
        error_message: str,
        error_type: str = "transient",
    ) -> dict[str, Any] | None:
        """原子收敛「预算已耗尽」的 QUEUED/RETRYING 到 DEAD_LETTER。

        预算耗尽必须**终态**收敛：``list_recoverable_runs`` 不比较
        ``max_attempts``，一个停在 QUEUED/RETRYING 且预算耗尽的 run 会被它反复
        重新投递，形成无界重投循环。

        谓词 ``status IN (...) AND attempt >= max_attempts`` 是条件比较，不依赖
        扫描时的 Python 快照。DLQ 行同事务写入。
        """
        now = now or _utcnow()
        allowed = [s.value for s in from_statuses]
        return self._dead_letter_conditionally(
            run_id=run_id,
            predicates=[
                AgentRun.id == run_id,
                AgentRun.status.in_(allowed),
                AgentRun.attempt >= AgentRun.max_attempts,
            ],
            error_code=error_code,
            error_message=error_message,
            error_type=error_type,
            now=now,
        )

    def cancel_if_not_terminal(self, run_id: str, *, now: datetime | None = None) -> bool:
        """原子取消：``id = run_id AND status NOT IN (终态)`` -> CANCELLED。

        不做「先 SELECT 读状态、再用该状态做 CAS」：读与写之间 run 可能已经从
        PENDING 被 promote 成 QUEUED（或从 QUEUED 被 worker 领取成 RUNNING），
        于是基于旧快照的 CAS 落空并抛 ``InvalidRunTransition``——一个纯粹的并发
        假失败。

        取消对**每个非终态**都合法，所以谓词可以直接写成「不是终态」，
        完全不需要预先知道当前状态。
        """
        now = now or _utcnow()
        terminal = [s.value for s in TERMINAL_STATUSES]
        session = self._session()
        try:
            result = session.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.status.notin_(terminal),
                )
                .values(
                    status=RunStatus.CANCELLED.value,
                    lease_expires_at=None,
                    next_retry_at=None,
                    finished_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

    # ---- PENDING 两阶段入队恢复 ----

    def list_stale_pending_runs(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
        grace_seconds: int = ORPHAN_GRACE_SECONDS,
    ) -> list[str]:
        """``PENDING`` 且已落库 ``grace_seconds`` 仍未变的 run。

        宽限期（不是无条件扫描）
        ----------------------
        ``PENDING`` 的语义是「已落库、投递结果未知」。进程可能在**消息仍在投递
        途中**这个窗口内被打断，此时立即重投只会制造一次多余投递。所以必须给一个
        宽限期：``updated_at``（PENDING 期间不会被别的路径改写，故等价于
        ``created_at``）早于 ``now - grace`` 才算「陈旧」。

        这样 PENDING 的实际恢复延迟是 ``grace + 一次扫描间隔``，不是扫描间隔本身。
        """
        now = now or _utcnow()
        cutoff = now - timedelta(seconds=max(0, int(grace_seconds)))
        stmt = (
            select(AgentRun.id)
            .where(
                AgentRun.status == RunStatus.PENDING.value,
                AgentRun.updated_at <= cutoff,
            )
            .order_by(AgentRun.updated_at.asc())
            .limit(max(1, int(limit)))
        )
        session = self._session()
        try:
            return [row[0] for row in session.execute(stmt).all()]
        finally:
            session.close()

    def promote_pending_to_queued(self, run_id: str, *, now: datetime | None = None) -> bool:
        """条件迁移 ``PENDING -> QUEUED``，返回是否真的迁移了。

        谓词 ``status = PENDING``：这是「必须先确认入库、再发消息」的**条件迁移**
        原语。

        - 两个 reconciler 同时扫到同一个陈旧 PENDING：只有一个 UPDATE 成功，
          只有一个投递（另一个看到 False 后跳过）；
        - PENDING 已被取消 / 已被 ``mark_queued`` 处理 / 已被别处改写：谓词不成立，
          **不会**发出消息；
        - 迁移成功但随后投递失败：run 停在 QUEUED 且 ``next_retry_at IS NULL``，
          由 ``list_recoverable_runs`` 的 QUEUED 孤儿分支兜底，最终一定收敛。
        """
        now = now or _utcnow()
        session = self._session()
        try:
            result = session.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.status == RunStatus.PENDING.value,
                )
                .values(
                    status=RunStatus.QUEUED.value,
                    queued_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

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
        self,
        *,
        limit: int = 100,
        now=None,
        orphan_grace_seconds: int = ORPHAN_GRACE_SECONDS,
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
