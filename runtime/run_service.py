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

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .repository import AgentRunRepository
from .statuses import (
    EXECUTABLE_STATUSES,
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


def build_idempotency_scope(user_id: str | None, endpoint: str, raw_key: str) -> str:
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
        """创建 QUEUED run；提供 idempotency_key（已作用域化）时命中已有 run 直接返回。"""
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
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        now = now or _utcnow()
        next_attempt = int(run["attempt"]) + 1

        if cur == RunStatus.RUNNING:
            if worker_id is None or lease_seconds is None:
                return run
            lease = _ensure_aware(run.get("lease_expires_at"))
            owner = run.get("worker_id")
            if lease is not None and lease > now and owner not in (None, worker_id):
                return None  # 其他 worker 持有有效 lease，重复投递不重复执行
            return self._transition(
                run_id,
                RunStatus.RUNNING,
                from_statuses={RunStatus.RUNNING},
                worker_id=worker_id,
                task_id=task_id,
                attempt=next_attempt,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
            )

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
    ) -> dict[str, Any]:
        """Permanent error -> FAILED（终态，不重试）。"""
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

    def mark_retrying(
        self,
        run_id: str,
        *,
        delay_seconds: float,
        error_type: str = "transient",
        error_code: str = "",
        error_message: str = "",
    ) -> dict[str, Any]:
        """Transient error -> RETRYING，写 next_retry_at（退避后重新投递）。"""
        run = self.require_run(run_id)
        ensure_transition(parse_status(run["status"]), RunStatus.RETRYING, run_id)
        now = _utcnow()
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

    def mark_dead_letter(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        error_type: str = "transient",
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """retry 用尽 -> DEAD_LETTER（终态）+ 持久 DLQ 记录。"""
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur in (RunStatus.RUNNING, RunStatus.RETRYING, RunStatus.QUEUED):
            updated = self.repo.transition(
                run_id,
                from_statuses={cur},
                to_status=RunStatus.DEAD_LETTER,
                error_code=error_code,
                error_message=error_message[:2000],
                error_type=error_type,
                last_error=error_message[:2000],
                lease_expires_at=None,
                finished_at=_utcnow(),
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
