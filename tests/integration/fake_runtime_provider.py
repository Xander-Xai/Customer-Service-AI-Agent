"""Deterministic fake runtime provider for distributed infrastructure tests.

Injected into the Celery worker via::

    AGENT_RUN_RUNTIME_PROVIDER=tests.integration.fake_runtime_provider:provide

It does NOT call any real LLM/provider. It exercises the full AgentRun state
machine (thread lock, lease, retry, DLQ, crash recovery) with a deterministic
slow graph stand-in so worker-loss/redelivery can be verified without
credentials. It records execution attempts to Redis for crash-boundary evidence.
"""

from __future__ import annotations

import os
import time

_COUNTER_KEY = "crash-test:executions"


def _redis_client():
    import redis

    url = os.getenv("REDIS_URL", "redis://localhost:6379")
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)


class _FakeRuntime:
    async def run(self, *, thread_id: str, query: str, user_id: str | None = None):
        sleep_seconds = float(os.getenv("FAKE_RUNTIME_SLEEP_SECONDS", "8"))
        try:
            client = _redis_client()
            client.incr(_COUNTER_KEY)
            client.hincrby("crash-test:exec-by-run", thread_id, 1)
        except Exception:
            client = None
        try:
            time.sleep(sleep_seconds)
        finally:
            if client is not None:
                client.close()
        return {
            "response": f"fake-ok:{query}",
            "thread_id": thread_id,
            "user_id": user_id,
            "agent": "fake",
        }


async def provide():
    return _FakeRuntime()
