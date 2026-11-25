"""Cross-process session admission, separate from mutable conversation metadata."""

from __future__ import annotations

import re
import uuid

import redis.asyncio as redis

from core.config import REDIS_URL

_SESSION = re.compile(r"^[a-zA-Z0-9_-]{1,36}$")
OWNER_TTL_SECONDS = 30 * 86400


class SessionOwnerUnavailable(RuntimeError):
    pass


class SessionOwnerDenied(PermissionError):
    pass


def _key(sid: str) -> str:
    if not _SESSION.fullmatch(sid):
        raise SessionOwnerDenied("Invalid session")
    return f"csai:industrial:owner:{sid}"


def _client():
    return redis.from_url(
        REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
    )


async def create_owned_session(user_id: str) -> str:
    if not user_id:
        raise SessionOwnerDenied("Authenticated user required")
    sid = str(uuid.uuid4())
    try:
        async with _client() as client:
            if not await client.set(_key(sid), str(user_id), nx=True, ex=OWNER_TTL_SECONDS):
                raise SessionOwnerUnavailable("Session registration failed")
    except SessionOwnerUnavailable:
        raise
    except Exception as exc:
        raise SessionOwnerUnavailable("Session ownership unavailable") from exc
    return sid


async def require_session_owner(sid: str, user_id: str | None) -> None:
    key = _key(sid)
    if not user_id:
        raise SessionOwnerDenied("Authenticated user required")
    try:
        async with _client() as client:
            owner = await client.get(key)
    except Exception as exc:
        raise SessionOwnerUnavailable("Session ownership unavailable") from exc
    if owner != str(user_id):
        raise SessionOwnerDenied("Session ownership not verified")
