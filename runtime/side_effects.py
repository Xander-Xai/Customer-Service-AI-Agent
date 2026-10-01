"""写操作工具（side effect）幂等存储。

at-least-once delivery + application-level idempotency：
同一 ``(tool_name, operation_key)`` 一旦 SUCCEEDED，重投递/崩溃恢复不得再次执行。
唯一约束在数据库层（``uq_tool_side_effects_operation``），Redis 不参与真相。

只读工具不经过此路径。
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    ) -> SideEffectClaim:
        """尝试声明一次副作用执行。

        - 已 SUCCEEDED 且指纹一致 -> succeeded（返回已存结果，不执行）；
        - 已 SUCCEEDED 但指纹不同 -> conflict（PermanentError）；
        - 不存在 / PENDING / FAILED -> execute（更新归属后执行）。
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
        if existing["status"] == STATUS_SUCCEEDED and existing["request_fingerprint"] == fingerprint:
            return SideEffectClaim(CLAIM_SUCCEEDED, existing["result_reference"])
        if existing["status"] == STATUS_SUCCEEDED:
            return SideEffectClaim(CLAIM_CONFLICT)
        return SideEffectClaim(CLAIM_EXECUTE)

    def mark_succeeded(
        self, tool_name: str, operation_key: str, result_reference: Any
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
