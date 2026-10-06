"""Scoped external storage for raw tool results.

The store is deliberately independent from the optimizer.  References are
opaque, records expire, and a caller must present the same scope that was used
when the result was stored.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


class ToolResultStoreError(RuntimeError):
    """A recoverable store failure; callers should fail soft."""


@dataclass(frozen=True)
class ToolResultRecord:
    reference_id: str
    tool_name: str
    payload: Any
    created_at: str
    expires_at: str
    scope: dict[str, str]
    schema_version: int = 1
    metadata: dict[str, str] | None = None


class ToolResultStoreProtocol(Protocol):
    async def put(
        self,
        tool_name: str,
        payload: Any,
        *,
        scope: dict[str, str],
        ttl_seconds: int,
        metadata: dict[str, str] | None = None,
    ) -> str: ...

    async def get(self, reference_id: str, *, scope: dict[str, str]) -> ToolResultRecord | None: ...

    async def delete(self, reference_id: str, *, scope: dict[str, str]) -> None: ...


def _scope_matches(expected: dict[str, str], actual: dict[str, str]) -> bool:
    return bool(expected) and expected == actual


class InMemoryToolResultStore:
    def __init__(self, *, clock=time.time) -> None:
        self._records: dict[str, tuple[ToolResultRecord, float]] = {}
        self._clock = clock

    async def put(self, tool_name, payload, *, scope, ttl_seconds, metadata=None) -> str:
        if ttl_seconds <= 0 or not scope:
            raise ValueError("ttl_seconds must be positive and scope must be non-empty")
        reference_id = "tr_" + secrets.token_urlsafe(18)
        now = self._clock()
        record = ToolResultRecord(
            reference_id=reference_id,
            tool_name=tool_name,
            payload=payload,
            created_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
            expires_at=datetime.fromtimestamp(now + ttl_seconds, timezone.utc).isoformat(),
            scope={str(k): str(v) for k, v in scope.items()},
            metadata=metadata,
        )
        self._records[reference_id] = (record, now + ttl_seconds)
        return reference_id

    async def get(self, reference_id, *, scope) -> ToolResultRecord | None:
        entry = self._records.get(reference_id)
        if entry is None:
            return None
        record, expires_at = entry
        if self._clock() >= expires_at:
            self._records.pop(reference_id, None)
            return None
        if not _scope_matches(record.scope, {str(k): str(v) for k, v in scope.items()}):
            return None
        return record

    async def delete(self, reference_id, *, scope) -> None:
        if await self.get(reference_id, scope=scope) is not None:
            self._records.pop(reference_id, None)


class RedisToolResultStore:
    """Redis-backed store using the application's existing Redis client/URL."""

    def __init__(
        self,
        redis_client=None,
        *,
        redis_url: str | None = None,
        key_prefix: str = "csai:tool-result:",
    ):
        self._client = redis_client
        self._redis_url = redis_url
        self._key_prefix = key_prefix

    def _get_client(self):
        if self._client is None:
            if not self._redis_url:
                raise ToolResultStoreError("Redis client is not configured")
            try:
                import redis

                self._client = redis.Redis.from_url(
                    self._redis_url, decode_responses=True, socket_timeout=2
                )
            except Exception as exc:
                raise ToolResultStoreError("Redis client initialization failed") from exc
        return self._client

    def _key(self, reference_id: str) -> str:
        if not reference_id.startswith("tr_") or len(reference_id) > 128:
            raise ValueError("invalid tool result reference")
        return self._key_prefix + reference_id

    async def put(self, tool_name, payload, *, scope, ttl_seconds, metadata=None) -> str:
        if ttl_seconds <= 0 or not scope:
            raise ValueError("ttl_seconds must be positive and scope must be non-empty")
        reference_id = "tr_" + secrets.token_urlsafe(18)
        now = time.time()
        record = ToolResultRecord(
            reference_id=reference_id,
            tool_name=tool_name,
            payload=payload,
            created_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
            expires_at=datetime.fromtimestamp(now + ttl_seconds, timezone.utc).isoformat(),
            scope={str(k): str(v) for k, v in scope.items()},
            metadata=metadata,
        )
        try:
            await asyncio.to_thread(
                self._get_client().setex,
                self._key(reference_id),
                ttl_seconds,
                json.dumps(asdict(record), ensure_ascii=False, default=str),
            )
        except Exception as exc:
            raise ToolResultStoreError("Redis tool result write failed") from exc
        return reference_id

    async def get(self, reference_id, *, scope) -> ToolResultRecord | None:
        try:
            raw = await asyncio.to_thread(self._get_client().get, self._key(reference_id))
            if not raw:
                return None
            data = json.loads(raw)
            record = ToolResultRecord(**data)
            if not _scope_matches(record.scope, {str(k): str(v) for k, v in scope.items()}):
                return None
            return record
        except ToolResultStoreError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise ToolResultStoreError("corrupt tool result record") from exc
        except Exception as exc:
            raise ToolResultStoreError("Redis tool result read failed") from exc

    async def delete(self, reference_id, *, scope) -> None:
        if await self.get(reference_id, scope=scope) is None:
            return
        try:
            await asyncio.to_thread(self._get_client().delete, self._key(reference_id))
        except Exception as exc:
            raise ToolResultStoreError("Redis tool result delete failed") from exc
