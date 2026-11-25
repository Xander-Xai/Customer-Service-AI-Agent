"""Distributed admission for industrial API graph executions, not socket count."""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import anyio
import redis.asyncio as redis

T = TypeVar("T")

# Both keys use the same Redis cluster hash tag. Only admitted requests are
# inserted; failed or crashed owners expire without a cleanup daemon.
ACQUIRE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
for i=1,2 do redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now) end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 2 end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return 3 end
local expires = now + tonumber(ARGV[3])
for i=1,2 do
    redis.call('ZADD', KEYS[i], expires, ARGV[4])
    redis.call('PEXPIRE', KEYS[i], tonumber(ARGV[3]) * 2)
end
return 1
"""
VALID = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
for i=1,2 do
    local expires = redis.call('ZSCORE', KEYS[i], ARGV[1])
    if not expires or tonumber(expires) <= now then return 0 end
end
return 1
"""
RELEASE = """
for i=1,2 do redis.call('ZREM', KEYS[i], ARGV[1]) end
return 1
"""


class FastPathAdmissionError(RuntimeError):
    def __init__(self, code: str, status: int):
        self.code = code
        self.status = status
        super().__init__(code)


def capacity_keys(user_id: str, secret: str) -> tuple[str, str]:
    if not user_id or not secret:
        raise ValueError("Missing admission identity")
    digest = hmac.digest(secret.encode(), user_id.encode(), "sha256").hex()
    return "csai:{industrial-fast}:global", f"csai:{{industrial-fast}}:user:{digest}"


def _limits() -> tuple[int, int, int]:
    from core.config import AGENT_RUN_THREAD_LOCK_TTL_SECONDS

    maximum = int(os.getenv("CSAI_FAST_PATH_MAX_ACTIVE", "32"))
    per_user = int(os.getenv("CSAI_FAST_PATH_MAX_ACTIVE_PER_USER", "2"))
    deadline = int(os.getenv("CSAI_FAST_PATH_DEADLINE_SECONDS", "90"))
    if not (1 <= per_user <= maximum <= 100000 and 1 <= deadline <= 600):
        raise ValueError("Invalid fast path configuration")
    if deadline + 10 >= AGENT_RUN_THREAD_LOCK_TTL_SECONDS:
        raise ValueError("Fast path deadline must leave thread lock margin")
    return maximum, per_user, deadline


async def execute_admitted(user_id: str | None, operation: Callable[[], Awaitable[T]]) -> T:
    """Apply shared capacity and a cooperative deadline to the graph boundary."""
    from core.config import JWT_SECRET, REDIS_URL

    try:
        maximum, per_user, deadline = _limits()
        keys = capacity_keys(user_id or "", JWT_SECRET)
    except (TypeError, ValueError) as exc:
        raise FastPathAdmissionError("FAST_PATH_UNAVAILABLE", 503) from exc

    token = secrets.token_hex(24)
    started = time.monotonic()
    ttl_ms = (deadline + 30) * 1000
    async with redis.from_url(
        REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
    ) as client:
        try:
            decision = await client.eval(ACQUIRE, 2, *keys, maximum, per_user, ttl_ms, token)
        except Exception as exc:
            raise FastPathAdmissionError("FAST_PATH_UNAVAILABLE", 503) from exc
        if decision == 2:
            raise FastPathAdmissionError("FAST_PATH_GLOBAL_BUSY", 503)
        if decision == 3:
            raise FastPathAdmissionError("FAST_PATH_USER_BUSY", 429)
        if type(decision) is not int or decision != 1:
            raise FastPathAdmissionError("FAST_PATH_UNAVAILABLE", 503)

        try:
            remaining = deadline - (time.monotonic() - started)
            if remaining <= 0:
                raise FastPathAdmissionError("FAST_PATH_DEADLINE", 504)
            try:
                result = await asyncio.wait_for(operation(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise FastPathAdmissionError("FAST_PATH_DEADLINE", 504) from exc
            try:
                valid = await client.eval(VALID, 2, *keys, token)
            except Exception as exc:
                raise FastPathAdmissionError("FAST_PATH_UNAVAILABLE", 503) from exc
            if type(valid) is not int or valid != 1:
                raise FastPathAdmissionError("FAST_PATH_LEASE_LOST", 503)
            if time.monotonic() - started >= deadline:
                raise FastPathAdmissionError("FAST_PATH_DEADLINE", 504)
            return result
        finally:
            # Join/cancellation is cooperative. Redis failure leaves an expiring
            # reservation rather than a permanent slot leak.
            with anyio.CancelScope(shield=True):
                try:
                    await client.eval(RELEASE, 2, *keys, token)
                except Exception:
                    pass
