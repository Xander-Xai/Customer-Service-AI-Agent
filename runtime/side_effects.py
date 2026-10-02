"""写操作工具（side effect）幂等 ledger。

at-least-once delivery + application-level idempotency：同一
``(tool_name, operation_key)`` 一旦 SUCCEEDED，重投递/崩溃恢复不得再次执行。
唯一约束在数据库层（``uq_tool_side_effects_operation``），Redis 不参与真相。

工具调用侧建议的 operation_key 约定::

    idempotency_key = run_id + ":" + tool_call_id

由 :func:`build_tool_idempotency_key` 构造，作为工具层接口边界。只读工具不经过
此路径。

边界：本 ledger 只防止「同一 Agent 重复发起同一副作用」。若下游外部系统需要
端到端幂等，必须由下游 API 接受 idempotency key，或配合人工对账。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.logger import get_logger
from db.database import get_db_session
from db.models import ToolSideEffect

logger = get_logger("runtime.side_effects")

STATUS_PENDING = "PENDING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"

CLAIM_EXECUTE = "execute"
CLAIM_SUCCEEDED = "succeeded"
CLAIM_CONFLICT = "conflict"
CLAIM_IN_PROGRESS = "in_progress"

#: PENDING 记录的认领租约：租约未过期时**不允许**第二个执行者重复触发副作用，
#: 必须抛 transient 让 run 退避重投；租约过期（认领后崩溃）才允许接管重放。
DEFAULT_CLAIM_TTL_SECONDS = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _claim_lease_expired(row: Any, ttl_seconds: float) -> bool:
    """认领租约是否已过期（认领者崩溃后可被接管重放）。

    优先使用显式的 ``claim_expires_at``；老数据没有该列时回退到 ``updated_at + ttl``。
    两者都缺失时视为**未过期**（保守：宁可让调用方退避重试，也不要误判成可接管而
    重复执行副作用）。
    """
    expires_at = getattr(row, "claim_expires_at", None)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return bool(_utcnow() >= expires_at)
    updated_at = getattr(row, "updated_at", None)
    if updated_at is None:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return bool((_utcnow() - updated_at).total_seconds() > max(0.0, ttl_seconds))


def _new_owner() -> str:
    """每次认领的唯一 owner token（host:pid:uuid 前缀便于排障）。"""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def build_tool_idempotency_key(run_id: str, tool_call_id: str) -> str:
    """工具侧幂等键 = run_id + tool_call_id（接口约定）。"""
    return f"{run_id}:{tool_call_id}"


def request_fingerprint(arguments: dict[str, Any] | None) -> str:
    payload = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_operation_key(tool_name: str, arguments: dict[str, Any] | None) -> str:
    """无显式 operation_key 时的兜底：对 tool_name + 参数取哈希。"""
    return f"{tool_name}:{request_fingerprint(arguments)}"


def _serialize_reference(result: Any) -> Any:
    if result is None or isinstance(result, dict | list | str | int | float | bool):
        return result
    return str(result)[:2000]


@dataclass
class SideEffectClaim:
    state: str  # execute | succeeded | conflict | in_progress
    result: Any = None
    #: 持有 CLAIM_EXECUTE 的认领 token；写结果时必须携带，用于归属校验。
    owner: str | None = None


class SideEffectStore:
    def __init__(self, session_factory: Callable[[], Any] | None = None):
        self._session_factory = session_factory or get_db_session

    def _session(self):
        return self._session_factory()

    def get(self, tool_name: str, operation_key: str) -> dict[str, Any] | None:
        session = self._session()
        try:
            row = session.execute(
                select(ToolSideEffect).where(
                    ToolSideEffect.tool_name == tool_name,
                    ToolSideEffect.operation_key == operation_key,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "tool_name": row.tool_name,
                "operation_key": row.operation_key,
                "status": row.status,
                "request_fingerprint": row.request_fingerprint,
                "result_reference": row.result_reference,
                "run_id": row.run_id,
            }
        finally:
            session.close()

    def claim(
        self,
        *,
        tool_name: str,
        operation_key: str,
        run_id: str,
        thread_id: str | None,
        fingerprint: str,
        claim_ttl_seconds: float = DEFAULT_CLAIM_TTL_SECONDS,
        owner: str | None = None,
    ) -> SideEffectClaim:
        """原子地声明一次副作用执行（**互斥**）。

        并发安全的关键（来自 PR review 的真实缺陷）：
          过去是 "SELECT -> 判断 -> UPDATE" 三步。两个 worker 可以同时读到同一条
          PENDING 行、同时判定可执行、同时拿到 ``CLAIM_EXECUTE``，于是同一笔退款被
          执行两次；唯一约束只防重复行，防不住重复执行。

        现在分三步，每步都是原子的：
          1. ``INSERT ... ON CONFLICT DO NOTHING``：不存在则原子创建并**立刻持有**
             认领；已存在则说明有人在处理；
          2. ``SELECT ... FOR UPDATE``：把竞争者串行化（行锁）；
          3. 持锁重读后判定：SUCCEEDED 同指纹 -> 返回已存结果；SUCCEEDED 异指纹 ->
             冲突；PENDING 且租约未过期 -> ``in_progress``；PENDING 租约已过期 /
             FAILED -> 改写 ``claim_owner`` 为自己并执行。

        ``owner`` 是本次认领的唯一 token（缺省自动生成）。只有持有当前 ``claim_owner``
        的执行者才能写入成功/失败结果，避免两个执行者互相覆盖。

        返回：
          - ``execute``      只有**一个**调用者会拿到
          - ``succeeded``    已完成，返回历史结果（不重复执行）
          - ``conflict``     同 key 不同参数
          - ``in_progress``  另一执行者持有未过期认领
        """
        owner = owner or _new_owner()
        now = _utcnow()
        session = self._session()
        try:
            # ---- 1) 原子插入（不存在时我们就是第一个持有者）----
            dialect = session.bind.dialect.name if session.bind is not None else ""
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                stmt = (
                    pg_insert(ToolSideEffect)
                    .values(
                        run_id=run_id,
                        thread_id=thread_id,
                        tool_name=tool_name,
                        operation_key=operation_key,
                        request_fingerprint=fingerprint,
                        status=STATUS_PENDING,
                        claim_owner=owner,
                        claim_expires_at=now + timedelta(seconds=claim_ttl_seconds),
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[ToolSideEffect.tool_name, ToolSideEffect.operation_key],
                    )
                    .returning(ToolSideEffect.id)
                )
                inserted_id = session.execute(stmt).scalar_one_or_none()
                if inserted_id is not None:
                    session.commit()
                    return SideEffectClaim(CLAIM_EXECUTE, owner=owner)
            else:
                # 非 PostgreSQL（SQLite/测试）：先查再插，唯一约束兜底
                existing = session.execute(
                    select(ToolSideEffect).where(
                        ToolSideEffect.tool_name == tool_name,
                        ToolSideEffect.operation_key == operation_key,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        ToolSideEffect(
                            run_id=run_id,
                            thread_id=thread_id,
                            tool_name=tool_name,
                            operation_key=operation_key,
                            request_fingerprint=fingerprint,
                            status=STATUS_PENDING,
                            claim_owner=owner,
                            claim_expires_at=now + timedelta(seconds=claim_ttl_seconds),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    try:
                        session.commit()
                        return SideEffectClaim(CLAIM_EXECUTE, owner=owner)
                    except IntegrityError:
                        session.rollback()

            # ---- 2) 持行锁串行化竞争者 ----
            row = session.execute(
                select(ToolSideEffect)
                .where(
                    ToolSideEffect.tool_name == tool_name,
                    ToolSideEffect.operation_key == operation_key,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                # 理论上不可达（唯一约束保证行存在）；保守起见不当成执行权。
                return SideEffectClaim(CLAIM_IN_PROGRESS, owner=None)

            # ---- 3) 持锁判定 ----
            if row.status == STATUS_SUCCEEDED:
                if row.request_fingerprint == fingerprint:
                    return SideEffectClaim(CLAIM_SUCCEEDED, row.result_reference, owner=None)
                return SideEffectClaim(CLAIM_CONFLICT, owner=None)

            lease_expired = _claim_lease_expired(row, claim_ttl_seconds)
            if row.status == STATUS_PENDING and not lease_expired:
                return SideEffectClaim(CLAIM_IN_PROGRESS, owner=None)

            # 接管（租约过期 = 原持有者已死；FAILED = 允许重试）
            row.run_id = run_id
            row.thread_id = thread_id
            row.status = STATUS_PENDING
            row.request_fingerprint = fingerprint
            row.claim_owner = owner
            row.claim_expires_at = now + timedelta(seconds=claim_ttl_seconds)
            row.updated_at = now
            session.commit()
            return SideEffectClaim(CLAIM_EXECUTE, owner=owner)
        except IntegrityError:
            # 并发插入（唯一约束兜底）：回滚后按"别人已持有"处理，绝不返回 EXECUTE。
            session.rollback()
            return SideEffectClaim(CLAIM_IN_PROGRESS, owner=None)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_succeeded(
        self,
        tool_name: str,
        operation_key: str,
        result_reference: Any,
        *,
        owner: str | None = None,
    ) -> bool:
        """标记成功。**只有当前认领者**可以写入；返回是否真的写入。

        归属校验解决"并发重试时后完成者覆盖先完成者结果"的问题：拿不到认领的调用方
        （owner 不匹配或已被接管）不会改写状态与结果。
        """
        session = self._session()
        try:
            stmt = select(ToolSideEffect).where(
                ToolSideEffect.tool_name == tool_name,
                ToolSideEffect.operation_key == operation_key,
            )
            if owner is not None:
                stmt = stmt.where(ToolSideEffect.claim_owner == owner)
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return False
            row.status = STATUS_SUCCEEDED
            row.result_reference = _serialize_reference(result_reference)
            row.error_type = None
            row.error_message = None
            row.finished_at = _utcnow()
            row.updated_at = _utcnow()
            row.claim_expires_at = None
            session.commit()
            return True
        finally:
            session.close()

    def mark_failed(
        self,
        tool_name: str,
        operation_key: str,
        *,
        error_type: str,
        error_message: str,
        owner: str | None = None,
    ) -> bool:
        """标记失败。**只有当前认领者**可以写入；返回是否真的写入。"""
        session = self._session()
        try:
            stmt = select(ToolSideEffect).where(
                ToolSideEffect.tool_name == tool_name,
                ToolSideEffect.operation_key == operation_key,
            )
            if owner is not None:
                stmt = stmt.where(ToolSideEffect.claim_owner == owner)
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return False
            row.status = STATUS_FAILED
            row.error_type = error_type
            row.error_message = error_message[:2000]
            row.updated_at = _utcnow()
            row.claim_expires_at = None
            session.commit()
            return True
        finally:
            session.close()


_default_store: SideEffectStore | None = None


def get_side_effect_store() -> SideEffectStore:
    global _default_store
    if _default_store is None:
        _default_store = SideEffectStore()
    return _default_store


def reset_side_effect_store_for_tests() -> None:
    global _default_store
    _default_store = None


async def execute_idempotent_operation(
    *,
    tool_name: str,
    operation_key: str,
    run_id: str,
    thread_id: str | None,
    arguments: dict[str, Any] | None,
    operation: Callable[[], Any],
    store: SideEffectStore | None = None,
    claim_ttl_seconds: float = DEFAULT_CLAIM_TTL_SECONDS,
    owner: str | None = None,
) -> Any:
    """通用写操作幂等包裹：同一 ``(tool_name, operation_key)`` 只真正执行一次。

    at-least-once execution + application-level idempotency：Tool 执行成功但
    LangGraph 后续失败、整个 run 重试时，第二次调用直接返回已保存结果，
    **不** 再次触发副作用函数。

    并发安全：唯一索引 + ``claim`` 的原子语义兜底；指纹不一致视为业务冲突
    （``PermanentError``，不重试）；认领租约未过期的重复认领抛 ``TransientError``
    （退避重投），避免两个 worker 并发执行同一笔写操作。
    """
    from .errors import PermanentError, TransientError

    store = store or get_side_effect_store()
    fingerprint = request_fingerprint(arguments)
    claim = store.claim(
        tool_name=tool_name,
        operation_key=operation_key,
        run_id=run_id,
        thread_id=thread_id,
        fingerprint=fingerprint,
        claim_ttl_seconds=claim_ttl_seconds,
        owner=owner,
    )
    if claim.state == CLAIM_SUCCEEDED:
        from . import metrics

        metrics.record_tool_idempotency_hit()
        return claim.result
    if claim.state == CLAIM_CONFLICT:
        raise PermanentError(
            f"tool {tool_name} operation_key {operation_key} 已以不同参数执行过"
        )
    if claim.state == CLAIM_IN_PROGRESS:
        raise TransientError(
            f"tool {tool_name} operation_key {operation_key} 正在被另一个执行者处理"
        )

    try:
        result = operation()
        if hasattr(result, "__await__"):
            result = await result
    except Exception as e:
        store.mark_failed(
            tool_name,
            operation_key,
            error_type=type(e).__name__,
            error_message=str(e),
            owner=claim.owner,
        )
        raise
    store.mark_succeeded(tool_name, operation_key, result, owner=claim.owner)
    return result
