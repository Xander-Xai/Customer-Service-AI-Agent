"""Exact, scoped cache for raw structured tool execution results.

This cache is intentionally separate from ToolResultStore: cache reuse avoids
executing a safe read again, while the store exists to recover an already
produced result by reference. Cache failures are non-fatal.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCachePolicy:
    enabled: bool = False
    ttl_seconds: int = 0
    cache_errors: bool = False
    cache_empty: bool = False
    require_scope: bool = True
    # Private resources (e.g. ERP order/customer) enforce an ownership check
    # inside the handler. A cache hit skips that handler, so a principal whose
    # authorization was revoked during the TTL would keep receiving the private
    # result — scope isolation is not authorization revocation. Such resources
    # must never be served from cache; the handler (and its authz boundary)
    # always runs.
    authorization_required: bool = False


def cacheable_result(result: Any, policy: ToolCachePolicy) -> bool:
    """Reject transient/error responses and empty values unless explicitly allowed."""
    if result is None:
        return False
    if result == [] or result == {} or result in ("", "查询完成，无结果"):
        return policy.cache_empty
    # ERP wrappers turn an empty adapter result (including a transient provider
    # outage that returned []) into a "未找到…" string. Caching that would keep
    # serving a false miss for the full TTL after the provider recovers, so it
    # is treated like an empty result (cache_empty, default False).
    if isinstance(result, str) and "未找到" in result:
        return policy.cache_empty
    if isinstance(result, str) and any(
        marker in result for marker in ("执行失败", "暂时不可用", "工具 '", "错误：工具")
    ):
        return policy.cache_errors
    return True


class ToolResultCacheProtocol(Protocol):
    async def get(
        self, tool_name: str, arguments: dict[str, Any], *, scope: dict[str, str]
    ) -> Any | None: ...

    async def set(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        *,
        scope: dict[str, str],
        ttl_seconds: int,
    ) -> None: ...

    async def invalidate(
        self, tool_name: str, arguments: dict[str, Any], *, scope: dict[str, str]
    ) -> None: ...


def canonical_tool_cache_key(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    scope: dict[str, str],
    schema_version: str = "tool-cache-v1",
) -> str:
    """Return a digest; raw arguments and identity values never enter the key text."""
    envelope = {
        "arguments": arguments,
        "schema_version": schema_version,
        "scope": scope,
        "tool_name": tool_name,
    }
    payload = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return "trc:" + hashlib.sha256(payload).hexdigest()


def _valid_scope(scope: dict[str, str]) -> bool:
    return bool(scope) and all(
        isinstance(key, str) and isinstance(value, str) and value for key, value in scope.items()
    )


class InMemoryToolResultCache:
    def __init__(self, *, clock=time.time) -> None:
        self._clock = clock
        self._entries: dict[str, tuple[Any, float]] = {}

    async def get(self, tool_name, arguments, *, scope):
        if not _valid_scope(scope):
            return None
        key = canonical_tool_cache_key(tool_name, arguments, scope=scope)
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._clock() >= expires_at:
            self._entries.pop(key, None)
            return None
        return value

    async def set(self, tool_name, arguments, result, *, scope, ttl_seconds):
        if not _valid_scope(scope) or ttl_seconds <= 0:
            return
        key = canonical_tool_cache_key(tool_name, arguments, scope=scope)
        self._entries[key] = (result, self._clock() + ttl_seconds)

    async def invalidate(self, tool_name, arguments, *, scope):
        if _valid_scope(scope):
            self._entries.pop(canonical_tool_cache_key(tool_name, arguments, scope=scope), None)


class RedisToolResultCache:
    """Redis implementation using the existing Redis client/URL seam."""

    def __init__(
        self,
        redis_client=None,
        *,
        redis_url: str | None = None,
        key_prefix: str = "csai:tool-cache:",
    ):
        self._client = redis_client
        self._redis_url = redis_url
        self._key_prefix = key_prefix

    def _get_client(self):
        if self._client is None:
            if not self._redis_url:
                raise RuntimeError("Redis client is not configured")
            import redis

            self._client = redis.Redis.from_url(
                self._redis_url, decode_responses=True, socket_timeout=2
            )
        return self._client

    def _key(self, tool_name, arguments, scope):
        return self._key_prefix + canonical_tool_cache_key(
            tool_name, arguments, scope=scope
        ).removeprefix("trc:")

    async def get(self, tool_name, arguments, *, scope):
        if not _valid_scope(scope):
            return None
        raw = await asyncio.to_thread(
            self._get_client().get, self._key(tool_name, arguments, scope)
        )
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, tool_name, arguments, result, *, scope, ttl_seconds):
        if not _valid_scope(scope) or ttl_seconds <= 0:
            return
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
        await asyncio.to_thread(
            self._get_client().setex, self._key(tool_name, arguments, scope), ttl_seconds, payload
        )

    async def invalidate(self, tool_name, arguments, *, scope):
        if _valid_scope(scope):
            await asyncio.to_thread(
                self._get_client().delete, self._key(tool_name, arguments, scope)
            )
