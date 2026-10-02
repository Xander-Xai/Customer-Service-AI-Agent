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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _claim_lease_expired(updated_at: datetime | None, ttl_seconds: float) -> bool:
    """PENDING 认领租约是否已过期（认领者崩溃后可被接管重放）。

    ``updated_at`` 为 None 视为**未过期**（保守：宁可让调用方退避重试，也不要
    误判成可接管而重复执行副作用）。
    """
    if updated_at is None:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (_utcnow() - updated_at).total_seconds() > max(0.0, ttl_seconds)


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
    if result is None or isinstance(result, (dict, list, str, int, float, bool)):
        return result
    return str(result)[:2000]


@dataclass
class SideEffectClaim:
    state: str  # execute | succeeded | conflict
    result: Any = None


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
    ) -> SideEffectClaim:
        """尝试声明一次副作用执行。

        - 已 SUCCEEDED 且指纹一致 -> succeeded（返回已存结果，不执行）；
        - 已 SUCCEEDED 但指纹不同 -> conflict；
        - PENDING 且认领租约**未过期** -> in_progress（另一执行者可能正在跑；
          重复执行会造成双重副作用，调用方须退避重试）；
        - PENDING 且租约**已过期**（认领后崩溃）-> execute（接管重放）；
        - FAILED / 不存在 -> execute。
        """
        session = self._session()
        try:
            row = session.execute(
                select(ToolSideEffect).where(
                    ToolSideEffect.tool_name == tool_name,
                    ToolSideEffect.operation_key == operation_key,
                )
            ).scalar_one_or_none()
            if row is not None:
                if row.status == STATUS_SUCCEEDED:
                    if row.request_fingerprint == fingerprint:
                        return SideEffectClaim(CLAIM_SUCCEEDED, row.result_reference)
                    return SideEffectClaim(CLAIM_CONFLICT)
                if row.status == STATUS_PENDING and not _claim_lease_expired(
                    row.updated_at, claim_ttl_seconds
                ):
                    return SideEffectClaim(CLAIM_IN_PROGRESS)
                row.run_id = run_id
                row.thread_id = thread_id
                row.status = STATUS_PENDING
                row.request_fingerprint = fingerprint
                row.updated_at = _utcnow()
                session.commit()
                return SideEffectClaim(CLAIM_EXECUTE)

            row = ToolSideEffect(
                run_id=run_id,
                thread_id=thread_id,
                tool_name=tool_name,
                operation_key=operation_key,
                request_fingerprint=fingerprint,
                status=STATUS_PENDING,
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            session.add(row)
            session.commit()
            return SideEffectClaim(CLAIM_EXECUTE)
        except IntegrityError:
            # 并发插入：回滚后按已有记录处理
            session.rollback()
        finally:
            session.close()

        existing = self.get(tool_name, operation_key)
        if existing is None:
            return SideEffectClaim(CLAIM_EXECUTE)
        if (
            existing["status"] == STATUS_SUCCEEDED
            and existing["request_fingerprint"] == fingerprint
        ):
            return SideEffectClaim(CLAIM_SUCCEEDED, existing["result_reference"])
        if existing["status"] == STATUS_SUCCEEDED:
            return SideEffectClaim(CLAIM_CONFLICT)
        return SideEffectClaim(CLAIM_EXECUTE)

    def mark_succeeded(self, tool_name: str, operation_key: str, result_reference: Any) -> None:
        session = self._session()
        try:
            row = session.execute(
                select(ToolSideEffect).where(
                    ToolSideEffect.tool_name == tool_name,
                    ToolSideEffect.operation_key == operation_key,
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.status = STATUS_SUCCEEDED
            row.result_reference = _serialize_reference(result_reference)
            row.error_type = None
            row.error_message = None
            row.finished_at = _utcnow()
            row.updated_at = _utcnow()
            session.commit()
        finally:
            session.close()

    def mark_failed(
        self,
        tool_name: str,
        operation_key: str,
        *,
        error_type: str,
        error_message: str,
    ) -> None:
        session = self._session()
        try:
            row = session.execute(
                select(ToolSideEffect).where(
                    ToolSideEffect.tool_name == tool_name,
                    ToolSideEffect.operation_key == operation_key,
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.status = STATUS_FAILED
            row.error_type = error_type
            row.error_message = error_message[:2000]
            row.updated_at = _utcnow()
            session.commit()
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
        )
        raise
    store.mark_succeeded(tool_name, operation_key, result)
    return result
