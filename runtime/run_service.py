"""AgentRun 服务层：所有状态迁移的唯一入口。

路由/worker 不得直接改数据库状态，必须经此服务，从而保证：
  - 合法迁移（``runtime/statuses.py``）；
  - 原子条件更新（并发 worker / 重复投递不会互相覆盖）；
  - 时间戳、错误分类、retry 调度一致。

可靠性语义：
  - ``attempt`` 在执行开始时（``mark_running``）递增，表示已开始的执行次数；
  - ``RUNNING`` 携带 worker_id + task_id + lease（ownership），lease 过期可被
    其他 worker 接管（崩溃恢复）；
  - transient error 走 ``RUNNING -> RETRYING`` 并写 ``next_retry_at``；
  - retry 用尽 / permanent error 写 ``DEAD_LETTER`` / ``FAILED``。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .repository import AgentRunRepository
from .statuses import (
    EXECUTABLE_STATUSES,
    TERMINAL_STATUSES,
    InvalidRunTransition,
    RunStatus,
    ensure_transition,
    parse_status,
)


class RunNotFound(LookupError):
    """指定 run_id 不存在。"""


class RunOwnershipLost(RuntimeError):
    """worker 已不再拥有该 run：提交被 owner CAS 拒绝。

    必须与 :class:`InvalidRunTransition` 区分。两者都是「写不进去」，但含义
    完全不同：

    - ``InvalidRunTransition``：状态机不接受这次迁移（例如 run 已 SUCCEEDED）。
    - ``RunOwnershipLost``：状态还是 RUNNING，但 ``worker_id`` 已经不是我了，
      或者我的 lease 已经过期。**这不是业务失败**。

    混淆两者会导致严重后果：一个失去所有权的 worker 会把「我不是 owner 了」
    当成 permanent error 去 ``mark_failed``，从而覆盖新 owner 正在跑的 run。
    所以这里刻意带 ``current_status``，让调用方能区分「状态不对」与
    「owner 不对」。

    只携带安全字段：run_id / 期望的 owner / 当前状态 / 当前 owner。不含 query、
    result、tool args 或任何用户数据。
    """

    def __init__(
        self,
        run_id: str,
        *,
        expected_worker_id: str | None,
        current_status: str | None = None,
        current_worker_id: str | None = None,
    ):
        self.run_id = run_id
        self.expected_worker_id = expected_worker_id
        self.current_status = current_status
        self.current_worker_id = current_worker_id
        super().__init__(
            f"run 所有权已丢失: run={run_id} "
            f"expected_worker={expected_worker_id} "
            f"current_status={current_status} current_worker={current_worker_id}"
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime | None) -> datetime | None:
    """SQLite 可能返回 naive datetime；统一为 UTC aware 以便比较。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _default_max_attempts() -> int:
    from core.config import AGENT_RUN_MAX_ATTEMPTS

    return AGENT_RUN_MAX_ATTEMPTS


#: ``agent_runs.idempotency_key`` 列宽（见 db/models.py / alembic 004）。
IDEMPOTENCY_KEY_MAX_LENGTH = 128


def build_idempotency_scope(user_id: str | None, endpoint: str, raw_key: str) -> str:
    """构造幂等作用域：user + endpoint + idempotency_key。

    不同用户/不同端点可复用同一个原始 key；同作用域内 DB 唯一约束保证只创建一个 run。

    作用域串长度不受调用方控制（``Idempotency-Key`` 请求头无长度上限），拼接后可能
    超过列宽 ``String(128)`` 并在 PostgreSQL 触发 ``value too long``。因此对拼接结果
    取 sha256，输出固定 64 字符：仍然确定性、仍然按 user+endpoint 分域，且永不溢出。
    """
    scope = f"{(user_id or 'anon')}:{endpoint}:{raw_key}"
    if len(scope) <= IDEMPOTENCY_KEY_MAX_LENGTH:
        return scope
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()


class RunService:
    def __init__(self, repository: AgentRunRepository | None = None):
        self.repo = repository or AgentRunRepository()

    # ---- 创建 / 查询 ----

    def create_run(
        self,
        *,
        query: str,
        session_id: str,
        user_id: str | None = None,
        thread_id: str | None = None,
        max_attempts: int | None = None,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
        status: str = RunStatus.QUEUED.value,
    ) -> dict[str, Any]:
        """创建 run；提供 idempotency_key（已作用域化）时命中已有 run 直接返回。

        默认状态 ``QUEUED``（保持既有 ``POST /api/runs`` 语义）；需要「先落库、后
        投递」的两阶段流程时传 ``status=RunStatus.PENDING``，投递成功后再
        ``mark_queued()``。
        """
        if idempotency_key:
            existing = self.repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
        return self.repo.create(
            run_id=str(uuid.uuid4()),
            thread_id=thread_id or session_id,
            session_id=session_id,
            query=query,
            user_id=user_id,
            max_attempts=(max_attempts if max_attempts is not None else _default_max_attempts()),
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            status=status,
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.repo.get(run_id)

    def get_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        return self.repo.get_by_idempotency_key(key)

    def require_run(self, run_id: str) -> dict[str, Any]:
        run = self.repo.get(run_id)
        if run is None:
            raise RunNotFound(run_id)
        return run

    def list_recoverable_runs(self, *, limit: int = 100, now: datetime | None = None) -> list[str]:
        """列出"卡住"的 run：RETRYING/QUEUED 且 next_retry_at 已到。

        用于重试投递失败后的兜底恢复（见 ``runtime/retry.py::reconcile_stuck_runs``）。
        """
        return self.repo.list_recoverable_runs(limit=limit, now=now or _utcnow())

    def get_dead_letter(self, run_id: str) -> dict[str, Any] | None:
        return self.repo.get_dead_letter(run_id)

    def list_dead_letters(self, limit: int = 100) -> list[dict[str, Any]]:
        """管理员观测：DEAD_LETTER 记录（脱敏由序列化层负责）。"""
        return self.repo.list_dead_letters(limit=limit)

    # ---- 状态迁移 ----

    def _transition(
        self,
        run_id: str,
        target: RunStatus,
        *,
        from_statuses: set[RunStatus] | frozenset[RunStatus],
        **fields: Any,
    ) -> dict[str, Any]:
        updated = self.repo.transition(
            run_id, from_statuses=from_statuses, to_status=target, **fields
        )
        if updated is None:
            current = self.repo.get(run_id)
            if current is None:
                raise RunNotFound(run_id)
            raise InvalidRunTransition(run_id, current["status"], target.value)
        return updated

    def _transition_owned(
        self,
        run_id: str,
        target: RunStatus,
        *,
        from_statuses: set[RunStatus] | frozenset[RunStatus],
        expected_worker_id: str,
        require_live_lease: bool = True,
        **fields: Any,
    ) -> dict[str, Any]:
        """worker-owned 提交：status + owner (+ 存活 lease) 同一条 UPDATE 判定。

        写不进去时区分三种原因：run 不存在 / 状态机不接受 / ownership 丢失。
        第三种抛 :class:`RunOwnershipLost` 而不是 ``InvalidRunTransition``，
        因为它不是业务失败。
        """
        updated = self.repo.transition_owned(
            run_id,
            from_statuses=from_statuses,
            to_status=target,
            expected_worker_id=expected_worker_id,
            require_live_lease=require_live_lease,
            **fields,
        )
        if updated is not None:
            return updated
        current = self.repo.get(run_id)
        if current is None:
            raise RunNotFound(run_id)
        if current["status"] not in {s.value for s in from_statuses}:
            raise InvalidRunTransition(run_id, current["status"], target.value)
        raise RunOwnershipLost(
            run_id,
            expected_worker_id=expected_worker_id,
            current_status=current["status"],
            current_worker_id=current.get("worker_id"),
        )

    def mark_running(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
        task_id: str | None = None,
        lease_seconds: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """QUEUED/RETRYING -> RUNNING（递增 attempt + 写 worker lease）。

        返回 None 表示「不应执行」：当前 RUNNING 且被其他 worker 的有效 lease
        持有（重复投递）。RUNNING 但 lease 过期/归属自己 -> 接管并递增 attempt。
        终态 -> InvalidRunTransition。

        RUNNING -> RUNNING 的接管是**单条 UPDATE 谓词**（见
        ``repository.takeover_running``），不是「先 SELECT 看到过期、再按 status
        更新」：后者允许两个竞争者读到同一份过期快照后都成功接管。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        now = now or _utcnow()
        next_attempt = int(run["attempt"]) + 1

        if cur == RunStatus.RUNNING:
            if worker_id is None or lease_seconds is None:
                return run
            taken = self.repo.takeover_running(
                run_id,
                worker_id=worker_id,
                task_id=task_id,
                lease_seconds=lease_seconds,
                now=now,
            )
            if taken is not None:
                return taken
            # 谓词不成立：要么别人持有有效 lease（重复投递），要么并发竞争输了。
            return None

        ensure_transition(cur, RunStatus.RUNNING, run_id)
        fields: dict[str, Any] = {
            "attempt": next_attempt,
            "worker_id": worker_id,
            "task_id": task_id,
        }
        if run.get("started_at") is None:
            fields["started_at"] = now
        if lease_seconds is not None:
            fields["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
            fields["heartbeat_at"] = now
        return self._transition(run_id, RunStatus.RUNNING, from_statuses={cur}, **fields)

    def heartbeat(self, run_id: str, *, worker_id: str, lease_seconds: float) -> bool:
        """续租（仅当前 owner 且 lease 仍存活时生效）。返回是否成功。

        原子 owner CAS：``id + status=RUNNING + worker_id + lease_expires_at > now``
        在同一条 UPDATE 里判定。旧实现是 ``get() -> Python 判 owner -> transition()``，
        那是 TOCTOU：读到自己的 owner 之后、写入之前，别人可能已经接管。

        两个后果都被这条谓词挡住：
          - 已被接管的 stale worker 续租失败，且不会改动新 owner 的任何字段；
          - **已过期但尚未被接管**的 owner 续租同样失败 —— 否则它能靠一次迟到的
            续租给自己续命，lease 就没有严格 expiry 语义了。
        """
        return self.repo.renew_lease_owned(
            run_id,
            expected_worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    def mark_succeeded(
        self,
        run_id: str,
        result: dict[str, Any] | None,
        *,
        expected_worker_id: str | None = None,
    ) -> dict[str, Any]:
        """RUNNING -> SUCCEEDED。

        ``expected_worker_id`` 由 worker 执行路径**必须**传入：只有仍然是当前
        owner 且 lease 存活时才允许提交，失去所有权抛 :class:`RunOwnershipLost`
        且不改动数据库。否则一个已被接管的旧 worker 迟到的成功会覆盖新 owner。

        传 ``None`` 表示「非 worker 提交的外部/管理写入」，保持原有只判 status
        的语义（测试与后台路径使用）。
        """
        if expected_worker_id is None:
            run = self.require_run(run_id)
            ensure_transition(parse_status(run["status"]), RunStatus.SUCCEEDED, run_id)
            return self._transition(
                run_id,
                RunStatus.SUCCEEDED,
                from_statuses={RunStatus.RUNNING},
                result=result or {},
                error_code=None,
                error_message=None,
                error_type=None,
                last_error=None,
                next_retry_at=None,
                lease_expires_at=None,
                finished_at=_utcnow(),
            )
        return self._transition_owned(
            run_id,
            RunStatus.SUCCEEDED,
            from_statuses={RunStatus.RUNNING},
            expected_worker_id=expected_worker_id,
            result=result or {},
            error_code=None,
            error_message=None,
            error_type=None,
            last_error=None,
            next_retry_at=None,
            lease_expires_at=None,
            finished_at=_utcnow(),
        )

    def mark_failed(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        error_type: str = "permanent",
        expected_worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Permanent error -> FAILED（终态，不重试）。

        worker 执行路径必须传 ``expected_worker_id``：stale worker 不能把新
        owner 正在跑的 RUNNING 改成 FAILED。
        """
        if expected_worker_id is None:
            run = self.require_run(run_id)
            ensure_transition(parse_status(run["status"]), RunStatus.FAILED, run_id)
            return self._transition(
                run_id,
                RunStatus.FAILED,
                from_statuses={RunStatus.RUNNING},
                error_code=error_code,
                error_message=error_message[:2000],
                error_type=error_type,
                last_error=error_message[:2000],
                lease_expires_at=None,
                finished_at=_utcnow(),
            )
        return self._transition_owned(
            run_id,
            RunStatus.FAILED,
            from_statuses={RunStatus.RUNNING},
            expected_worker_id=expected_worker_id,
            error_code=error_code,
            error_message=error_message[:2000],
            error_type=error_type,
            last_error=error_message[:2000],
            lease_expires_at=None,
            finished_at=_utcnow(),
        )

    def mark_retrying(
        self,
        run_id: str,
        *,
        delay_seconds: float,
        error_type: str = "transient",
        error_code: str = "",
        error_message: str = "",
        expected_worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Transient error -> RETRYING，写 next_retry_at（退避后重新投递）。

        worker 执行路径必须传 ``expected_worker_id``：否则 stale worker 可以在
        新 owner 执行期间把 RUNNING 改成 RETRYING，并让上层发布一个不该存在的
        重试消息。
        """
        now = _utcnow()
        if expected_worker_id is None:
            run = self.require_run(run_id)
            ensure_transition(parse_status(run["status"]), RunStatus.RETRYING, run_id)
            return self._transition(
                run_id,
                RunStatus.RETRYING,
                from_statuses={RunStatus.RUNNING},
                next_retry_at=now + timedelta(seconds=max(0.0, delay_seconds)),
                error_type=error_type,
                error_code=error_code or None,
                last_error=error_message[:2000] or None,
                lease_expires_at=None,
            )
        return self._transition_owned(
            run_id,
            RunStatus.RETRYING,
            from_statuses={RunStatus.RUNNING},
            expected_worker_id=expected_worker_id,
            next_retry_at=now + timedelta(seconds=max(0.0, delay_seconds)),
            error_type=error_type,
            error_code=error_code or None,
            last_error=error_message[:2000] or None,
            lease_expires_at=None,
        )

    def mark_dead_letter(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        error_type: str = "transient",
        worker_id: str | None = None,
        expected_worker_id: str | None = None,
    ) -> dict[str, Any]:
        """retry 用尽 -> DEAD_LETTER（终态）+ 持久 DLQ 记录。

        按来源区分 ownership（刻意不一刀切）：

        - **RUNNING 来源**：这是 worker 执行路径提交的结果，必须 owner CAS。
          传 ``expected_worker_id`` 生效；stale worker 不能把新 owner 的 run
          推入 DLQ。
        - **RETRYING / QUEUED 来源**：DLQ 重放、reconciler、管理员/API 路径
          （例如 ``POST /api/runs`` 的 dispatch 失败），它们不是 worker ownership
          commit，**不**要求 worker_id 匹配。若一并 owner-gate 会直接打断运维
          闭环。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        owner_gated = cur == RunStatus.RUNNING and expected_worker_id is not None
        if cur in (RunStatus.RUNNING, RunStatus.RETRYING, RunStatus.QUEUED):
            fields = {
                "error_code": error_code,
                "error_message": error_message[:2000],
                "error_type": error_type,
                "last_error": error_message[:2000],
                "lease_expires_at": None,
                "finished_at": _utcnow(),
            }
            if owner_gated:
                updated = self._transition_owned(
                    run_id,
                    RunStatus.DEAD_LETTER,
                    from_statuses={RunStatus.RUNNING},
                    expected_worker_id=expected_worker_id,
                    **fields,
                )
            else:
                updated = self.repo.transition(
                    run_id,
                    from_statuses={cur},
                    to_status=RunStatus.DEAD_LETTER,
                    **fields,
                )
                if updated is None:
                    latest = self.repo.get(run_id)
                    if latest is None:
                        raise RunNotFound(run_id)
                    if latest["status"] == RunStatus.DEAD_LETTER.value:
                        updated = latest
                    else:
                        raise InvalidRunTransition(run_id, latest["status"], "DEAD_LETTER")
        elif cur == RunStatus.DEAD_LETTER:
            updated = run
        else:
            raise InvalidRunTransition(run_id, cur.value, RunStatus.DEAD_LETTER.value)

        self.repo.add_dead_letter(
            run_id=run_id,
            thread_id=run["thread_id"],
            attempt_count=int(run["attempt"]),
            max_attempts=int(run["max_attempts"]),
            error_type=error_type,
            error_code=error_code,
            error_message=error_message,
            worker_id=worker_id or run.get("worker_id"),
        )
        return updated

    def requeue_dead_letter(self, run_id: str, *, reset_attempts: bool = True) -> dict[str, Any]:
        """把 DEAD_LETTER run 重新投递（人工 replay / redrive）。

        刻意**复用原 run_id**，而不是复制出一个新 run：
          - 工具幂等键 ``operation_key = run_id:tool_call_id`` 依赖 run_id 稳定。换
            新 run_id 会让重放绕过 side-effect ledger，把已经成功写入外部系统的
            退款/改单**再执行一次**；这比重放失败严重得多。
          - 规范里的 ``job_id`` 是「一次队列投递」，重放正是新的一次投递，与
            run_id 是两个概念。

        历史保留：``agent_dead_letters`` 行不可变（``add_dead_letter`` 对既有行
        直接返回），因此原始 attempt / error_code / error_type / entered_at 全部留存。
        仅重置可重试字段与 attempt 计数。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur != RunStatus.DEAD_LETTER:
            raise InvalidRunTransition(run_id, cur.value, RunStatus.QUEUED.value)
        fields: dict[str, Any] = {
            "next_retry_at": None,
            "lease_expires_at": None,
            "error_code": None,
            "error_message": None,
            "error_type": None,
            "last_error": None,
            "finished_at": None,
            "worker_id": None,
            "task_id": None,
        }
        if reset_attempts:
            fields["attempt"] = 0
        queued = self._transition(
            run_id,
            RunStatus.QUEUED,
            from_statuses={RunStatus.DEAD_LETTER},
            **fields,
        )
        queued_at = queued.get("queued_at")
        if queued_at is not None:
            # 重新入队时间单独刷新，保留首次 queued_at 作为延迟统计起点
            self.repo.touch_queued(run_id, _utcnow())
        return self.repo.get(run_id) or queued

    def defer_run(self, run_id: str, *, delay_seconds: float) -> dict[str, Any] | None:
        """thread 竞争时延迟重调度：保持 QUEUED/RETRYING，仅更新 next_retry_at。

        不消耗 attempt（竞争不是 run 失败）。
        """
        run = self.repo.get(run_id)
        if run is None:
            return None
        cur = parse_status(run["status"])
        if cur not in EXECUTABLE_STATUSES:
            return run
        now = _utcnow()
        return self._transition(
            run_id,
            cur,
            from_statuses={cur},
            next_retry_at=now + timedelta(seconds=max(0.0, delay_seconds)),
        )

    def mark_queued(self, run_id: str) -> dict[str, Any]:
        """PENDING -> QUEUED（两阶段创建的入队确认）。"""
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur == RunStatus.QUEUED:
            return run
        ensure_transition(cur, RunStatus.QUEUED, run_id)
        return self._transition(
            run_id,
            RunStatus.QUEUED,
            from_statuses={cur},
            queued_at=_utcnow(),
        )

    def mark_waiting_approval(
        self, run_id: str, *, expected_worker_id: str | None = None
    ) -> dict[str, Any]:
        """RUNNING -> WAITING_APPROVAL：高风险副作用已挂起等人工决策。

        幂等：已在 WAITING_APPROVAL 直接返回；在终态或 QUEUED/RETRYING 时不改写
        （抛 ``InvalidRunTransition`` 由调用方收敛）。

        刻意**清空 lease 与 worker_id**：图已挂起、没有 worker 在执行，留着过期
        lease 只会让「谁有权接管」的判断失真；清空后恢复路径可以干净地重新领取。
        也因此 approval 恢复（``mark_resumed_running``）由第一个 worker 通过
        status CAS 领取新 ownership，**不**要求匹配审批前的 worker。

        worker 执行路径必须传 ``expected_worker_id``：失去所有权的旧 worker 不能
        把新 owner 的 run 挂起等审批。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur == RunStatus.WAITING_APPROVAL:
            return run
        ensure_transition(cur, RunStatus.WAITING_APPROVAL, run_id)
        fields: dict[str, Any] = {
            "lease_expires_at": None,
            "next_retry_at": None,
            "worker_id": None,
        }
        if expected_worker_id is None:
            return self._transition(
                run_id,
                RunStatus.WAITING_APPROVAL,
                from_statuses={cur},
                **fields,
            )
        return self._transition_owned(
            run_id,
            RunStatus.WAITING_APPROVAL,
            from_statuses={RunStatus.RUNNING},
            expected_worker_id=expected_worker_id,
            **fields,
        )

    def mark_resumed_running(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
        task_id: str | None = None,
        lease_seconds: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """WAITING_APPROVAL -> RUNNING（人工审批已决策，恢复执行）。**不递增 attempt。**

        为什么必须与 ``mark_running`` 分开
        --------------------------------
        ``mark_running`` 语义是「一次执行尝试」，每次领取都 ``attempt + 1``。
        审批恢复**不是**新的一次失败重试：等人可能耗时数小时，若复用
        ``mark_running``，``AGENT_RUN_MAX_ATTEMPTS``（默认 3）会被「等待审批」
        消耗掉，审批落地前 run 就已 DEAD_LETTER。等待人不是失败。

        并发：条件更新 ``WHERE status='WAITING_APPROVAL'``，多个 worker 同时
        恢复只有一个成功；其余收到 ``InvalidRunTransition`` 并读到 RUNNING。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur == RunStatus.RUNNING:
            return run
        ensure_transition(cur, RunStatus.RUNNING, run_id)
        now = now or _utcnow()
        fields: dict[str, Any] = {
            "worker_id": worker_id,
            "task_id": task_id,
            # attempt 原样保留：审批等待不消耗重试预算
        }
        if lease_seconds is not None:
            fields["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
            fields["heartbeat_at"] = now
        return self._transition(
            run_id, RunStatus.RUNNING, from_statuses={RunStatus.WAITING_APPROVAL}, **fields
        )

    def resume_after_approval(self, run_id: str) -> dict[str, Any]:
        """等待审批的 run 是否已可恢复（供审批 API 判断能否投递）。

        返回该 run 的最新记录；**不改状态**。恢复由 worker 领取时走
        ``mark_resumed_running`` 完成（``WAITING_APPROVAL -> RUNNING``）。

        这里刻意**没有** ``WAITING_APPROVAL -> QUEUED`` 这条边：审批 API 若把 run
        改成 QUEUED，worker 领取就会走 ``mark_running``，而它每次领取都
        ``attempt + 1``；``AGENT_RUN_MAX_ATTEMPTS`` 默认 3，于是「等人审批」会
        被当成三次失败重试，审批还没落地 run 就 DEAD_LETTER。等待人不是失败。
        审批 API 只负责 dispatch，状态由 worker 侧原子迁移。
        """
        return self.require_run(run_id)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """取消尚未完成的 run -> CANCELLED（终态）。

        语义边界（必须诚实说明）：
          - 已进入终态（SUCCEEDED/FAILED/DEAD_LETTER/CANCELLED）-> 不改写，返回现状；
          - QUEUED/RETRYING/PENDING -> 立即置 CANCELLED：worker 领取时会因状态不是
            EXECUTABLE_STATUSES 而跳过，等价于「投递后被丢弃」，不会执行图；
          - RUNNING -> 置 CANCELLED 属于**协作式取消**：只改状态，不中断已在运行的
            asyncio task。worker 的 ``mark_succeeded`` 走
            ``from_statuses={RUNNING}`` 条件更新，因此不会覆盖 CANCELLED；调用方
            必须等待 ``finished_at``/轮询确认最终态，不能假设已中断执行。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur in TERMINAL_STATUSES:
            return run
        now = _utcnow()
        return self._transition(
            run_id,
            RunStatus.CANCELLED,
            from_statuses={cur},
            lease_expires_at=None,
            next_retry_at=None,
            finished_at=now,
        )


_default_service: RunService | None = None


def get_run_service() -> RunService:
    """进程内默认服务（路由使用；测试可注入独立 service）。"""
    global _default_service
    if _default_service is None:
        _default_service = RunService()
    return _default_service


def reset_run_service_for_tests() -> None:
    global _default_service
    _default_service = None
