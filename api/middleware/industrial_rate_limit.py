"""Atomic, fail-closed HTTP admission for the industrial runtime."""

from __future__ import annotations

import hmac
import secrets

import redis.asyncio as redis

# Redis server time avoids application clock skew. Only admitted requests
# consume space; denial neither inserts a member nor extends its lifetime.
SLIDING_WINDOW = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000000 + tonumber(clock[2])
local window = tonumber(ARGV[2]) * 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
    return 0
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
"""


def admission_key(client_ip: str, scope: str, secret: str) -> str:
    if not secret or scope not in {"http", "login", "register"}:
        raise ValueError("Invalid admission configuration")
    digest = hmac.digest(secret.encode(), client_ip.encode(), "sha256").hex()
    return f"csai:industrial:rate:{scope}:{digest}"


async def admit_http(client_ip: str, scope: str, maximum: int, window: int) -> bool | None:
    """True/False is a Redis decision; None means unavailable, never allow."""
    from core.config import JWT_SECRET, REDIS_URL

    if maximum < 1 or window < 1:
        return None
    try:
        key = admission_key(client_ip, scope, JWT_SECRET)
        async with redis.from_url(
            REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
        ) as client:
            result = await client.eval(
                SLIDING_WINDOW, 1, key, maximum, window, secrets.token_hex(16)
            )
        if result not in (0, 1):
            return None
        return result == 1
    except Exception:
        return None
