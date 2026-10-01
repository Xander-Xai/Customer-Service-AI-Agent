"""AgentRun 服务层：所有状态迁移的唯一入口。

路由/worker 不得直接改数据库状态，必须经此服务，从而保证：
  - 合法迁移（``runtime/statuses.py``）；
  - 原子条件更新（并发 cancel vs worker 不会互相覆盖）；
  - 时间戳、错误分类、retry 调度一致。

可靠性要点：
  - idempotency_key 作用域由调用方用 :func:`build_idempotency_scope` 构造；
  - RUNNING 携带 worker_id + lease（ownership），过期可被其他 worker 接管；
  - retry 走显式 ``FAILED -> QUEUED(attempt+1)`` 并写 ``next_retry_at``。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .repository import AgentRunRepository
from .statuses import (
    CANCELLABLE_STATUSES,
    InvalidRunTransition,
    RunStatus,
    ensure_transition,
    parse_status,
)


class RunNotFound(LookupError):
    """指定 run_id 不存在。"""


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


def build_idempotency_scope(
    user_id: str | None, endpoint: str, raw_key: str
) -> str:
    """构造幂等作用域：user + endpoint + idempotency_key。

    不同用户/不同端点可复用同一个原始 key；同作用域内 DB 唯一约束保证只创建一个 run。
    """
    return f"{(user_id or 'anon')}:{endpoint}:{raw_key}"


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
    ) -> dict[str, Any]:
        """创建 PENDING run；提供 idempotency_key（已作用域化）时命中已有 run 直接返回。"""
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
            max_attempts=max_attempts if max_attempts is not None else _default_max_attempts(),
            trace_id=trace_id,
            idempotency_key=idempotency_key,
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

    def list_dead(self, limit: int = 100) -> list[dict[str, Any]]:
        """管理员观测：DEAD run 列表（脱敏由序列化层负责）。"""
        return self.repo.list_by_status(RunStatus.DEAD, limit=limit)

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

    def mark_queued(self, run_id: str) -> dict[str, Any]:
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

    def mark_running(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """QUEUED/PENDING -> RUNNING（带 worker lease）。

        - 已是 RUNNING 且 lease 有效、归属其他 worker -> 返回 None（不应执行）；
        - 已是 RUNNING 且 lease 过期/同 worker -> 续租并返回；
        - 终态/FAILED -> InvalidRunTransition。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        now = now or _utcnow()

        if cur == RunStatus.RUNNING:
            if worker_id is None or lease_seconds is None:
                return run
            lease = _ensure_aware(run.get("lease_expires_at"))
            owner = run.get("worker_id")
            if lease is not None and lease > now and owner not in (None, worker_id):
                return None  # 其他 worker 持有有效 lease
            return self._transition(
                run_id,
                RunStatus.RUNNING,
                from_statuses={RunStatus.RUNNING},
                worker_id=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
            )

        ensure_transition(cur, RunStatus.RUNNING, run_id)
        fields: dict[str, Any] = {"started_at": now}
        if worker_id is not None:
            fields["worker_id"] = worker_id
        if lease_seconds is not None:
            fields["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
            fields["heartbeat_at"] = now
        return self._transition(run_id, RunStatus.RUNNING, from_statuses={cur}, **fields)

    def heartbeat(self, run_id: str, *, worker_id: str, lease_seconds: float) -> bool:
        """续租（仅当前 owner 生效）。返回是否成功。"""
        run = self.repo.get(run_id)
        if run is None or run["status"] != RunStatus.RUNNING.value:
            return False
        if run.get("worker_id") not in (None, worker_id):
            return False
        now = _utcnow()
        updated = self.repo.transition(
            run_id,
            from_statuses={RunStatus.RUNNING},
            to_status=RunStatus.RUNNING,
            worker_id=worker_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            heartbeat_at=now,
        )
        return updated is not None

    def mark_succeeded(self, run_id: str, result: dict[str, Any] | None) -> dict[str, Any]:
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
            finished_at=_utcnow(),
        )

    def mark_failed(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        error_type: str = "transient",
    ) -> dict[str, Any]:
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
            finished_at=_utcnow(),
        )

    def schedule_retry(
        self,
        run_id: str,
        *,
        delay_seconds: float = 0.0,
        error_type: str = "transient",
        error_message: str = "",
    ) -> dict[str, Any]:
        """显式 retry：FAILED -> QUEUED 且 attempt+1，写 next_retry_at。

        attempts 用尽时抛 InvalidRunTransition（调用方应 mark_dead）。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur != RunStatus.FAILED:
            raise InvalidRunTransition(run_id, cur.value, RunStatus.QUEUED.value)
        next_attempt = int(run["attempt"]) + 1
        if next_attempt >= int(run["max_attempts"]):
            raise InvalidRunTransition(
                run_id, cur.value, f"{RunStatus.QUEUED.value}(attempts exhausted)"
            )
        now = _utcnow()
        return self._transition(
            run_id,
            RunStatus.QUEUED,
            from_statuses={RunStatus.FAILED},
            attempt=next_attempt,
            queued_at=now,
            next_retry_at=now + timedelta(seconds=max(0.0, delay_seconds)),
            error_type=error_type,
            last_error=error_message[:2000] or None,
            finished_at=None,
        )

    def retry_run(self, run_id: str) -> dict[str, Any]:
        """向后兼容别名：立即 retry（无延迟）。"""
        return self.schedule_retry(run_id, delay_seconds=0.0)

    def mark_dead(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        error_type: str = "permanent",
    ) -> dict[str, Any]:
        """终态失败（不可重试 / attempts 用尽）。"""
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        ensure_transition(cur, RunStatus.DEAD, run_id)
        return self._transition(
            run_id,
            RunStatus.DEAD,
            from_statuses={cur},
            error_code=error_code,
            error_message=error_message[:2000],
            error_type=error_type,
            last_error=error_message[:2000],
            finished_at=_utcnow(),
        )

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """取消尚未开始执行的 run（PENDING/QUEUED）。"""
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur not in CANCELLABLE_STATUSES:
            raise InvalidRunTransition(run_id, cur.value, RunStatus.CANCELLED.value)
        return self._transition(
            run_id,
            RunStatus.CANCELLED,
            from_statuses={cur},
            finished_at=_utcnow(),
        )

    def mark_cancelled(self, run_id: str, *, reason: str = "") -> dict[str, Any]:
        """执行中收到取消信号（RUNNING -> CANCELLED）。"""
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        ensure_transition(cur, RunStatus.CANCELLED, run_id)
        return self._transition(
            run_id,
            RunStatus.CANCELLED,
            from_statuses={cur},
            last_error=reason[:2000] or None,
            finished_at=_utcnow(),
        )

    def mark_waiting_approval(self, run_id: str) -> dict[str, Any]:
        """高风险操作触发人工审批（RUNNING -> WAITING_APPROVAL）。

        Graph 被 checkpoint 暂停；审批决定后由 ``mark_resumed`` 重新入队恢复。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        ensure_transition(cur, RunStatus.WAITING_APPROVAL, run_id)
        return self._transition(
            run_id,
            RunStatus.WAITING_APPROVAL,
            from_statuses={cur},
        )

    def mark_resumed(self, run_id: str) -> dict[str, Any]:
        """审批完成，恢复执行（WAITING_APPROVAL -> QUEUED）。"""
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur != RunStatus.WAITING_APPROVAL:
            raise InvalidRunTransition(run_id, cur.value, RunStatus.QUEUED.value)
        return self._transition(
            run_id,
            RunStatus.QUEUED,
            from_statuses={RunStatus.WAITING_APPROVAL},
            queued_at=_utcnow(),
        )

    def defer_run(self, run_id: str, *, delay_seconds: float) -> dict[str, Any] | None:
        """thread 竞争时延迟重调度：保持 QUEUED/PENDING，仅更新 next_retry_at。

        不消耗 attempt（竞争不是 run 失败）。
        """
        run = self.repo.get(run_id)
        if run is None:
            return None
        cur = parse_status(run["status"])
        if cur not in (RunStatus.QUEUED, RunStatus.PENDING):
            return run
        now = _utcnow()
        return self._transition(
            run_id,
            cur,
            from_statuses={cur},
            next_retry_at=now + timedelta(seconds=max(0.0, delay_seconds)),
        )


_default_service: RunService | None = None


def get_run_service() -> RunService:
    """进程内默认服务（路由使用；测试可注入独立 service）。"""
    global _default_service
    if _default_service is None:
        _default_service = RunService()
    return _default_service
