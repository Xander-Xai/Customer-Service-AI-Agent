"""Shared provider/model token-bucket admission for real HTTP attempts.

The global and user buckets share a Redis Cluster hash tag so one Lua operation
can make an all-or-nothing decision without unbounded user fields in one hash.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os

import redis.asyncio as redis

ADMIT = r"""
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local tpm_capacity = tonumber(ARGV[11])
local user_rpm_capacity = tonumber(ARGV[3])
local user_rpm_refill = tonumber(ARGV[4])
local user_tpm_capacity = tonumber(ARGV[5])
local user_tpm_refill = tonumber(ARGV[6])
local cost = tonumber(ARGV[7])
local user_cost = tonumber(ARGV[8])
local ttl = tonumber(ARGV[10])
local fields = {
  {1, 'rpm', capacity, refill, 1},
  {1, 'tpm', tpm_capacity, tpm_capacity / 60, cost},
  {2, 'rpm', user_rpm_capacity, user_rpm_refill, 1},
  {2, 'tpm', user_tpm_capacity, user_tpm_refill, user_cost}
}
local tokens = {}
local updated = {}
local waits = {}
for i, item in ipairs(fields) do
  local balance = tonumber(redis.call('HGET', KEYS[item[1]], item[2]))
  local last = tonumber(redis.call('HGET', KEYS[item[1]], item[2] .. ':at'))
  if not balance then balance = item[3] end
  if not last then last = now end
  -- Redis TIME is shared. A backward clock step pauses refill; a forward jump
  -- is bounded by capacity rather than manufacturing more than a full bucket.
  local elapsed = math.max(0, math.min(now - last, item[3] / item[4]))
  balance = math.min(item[3], balance + elapsed * item[4])
  tokens[i] = balance
  updated[i] = now
  waits[i] = math.max(0, (item[5] - balance) / item[4])
end
local retry = 0
for i, item in ipairs(fields) do
  if item[5] > item[3] then return {-1, 0} end
  if tokens[i] < item[5] then retry = math.max(retry, waits[i]) end
end
if retry > 0 then return {0, math.ceil(retry)} end
for i, item in ipairs(fields) do
  redis.call('HSET', KEYS[item[1]], item[2], tokens[i] - item[5], item[2] .. ':at', updated[i])
end
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return {1, 0}
"""


class LLMAdmissionRejected(RuntimeError):
    def __init__(self, retry_after: int | None, reason: str = "provider_model_capacity"):
        self.retry_after = max(1, retry_after) if retry_after is not None else None
        self.reason = reason
        super().__init__(reason)


class LLMAdmissionUnavailable(RuntimeError):
    pass


_client: redis.Redis | None = None


async def close_admission_client() -> None:
    global _client
    client, _client = _client, None
    if client is not None:
        await client.aclose()


def _settings() -> tuple[int, int, int, int, int]:
    # These are explicitly local-development defaults. Industrial deployments
    # must provide provider-agreed values instead of inheriting example limits.
    from core.industrial import industrial_enabled

    names = (
        "CSAI_LLM_RPM",
        "CSAI_LLM_TPM",
        "CSAI_LLM_USER_RPM",
        "CSAI_LLM_USER_TPM",
        "CSAI_LLM_BUCKET_TTL_SECONDS",
    )
    if industrial_enabled() and any(not os.getenv(name) for name in names):
        raise LLMAdmissionUnavailable("Industrial LLM admission limits are not configured")
    values = tuple(
        int(os.getenv(name, default))
        for name, default in zip(names, (60, 120000, 6, 12000, 7200), strict=True)
    )
    if any(value <= 0 for value in values):
        raise LLMAdmissionUnavailable("Invalid LLM admission limits")
    return values[0], values[1], values[2], values[3], values[4]


async def admit_provider_attempt(
    *,
    base_url: str,
    model: str,
    user_id: str | None,
    estimated_tokens: int,
    client_override: redis.Redis | None = None,
) -> None:
    """Admit one provider HTTP attempt or fail closed in industrial mode."""
    from core.config import JWT_SECRET, REDIS_URL
    from core.industrial import industrial_enabled

    if not industrial_enabled():
        return
    if not user_id or estimated_tokens <= 0:
        raise LLMAdmissionUnavailable("Trusted user identity and token estimate are required")
    try:
        rpm, tpm, user_rpm, user_tpm, ttl = _settings()
        secret = JWT_SECRET.encode()
        scope = hashlib.sha256((base_url + "\0" + model).encode()).hexdigest()[:32]
        user = hmac.digest(secret, user_id.encode(), "sha256").hex()[:32]
        key = f"csai:{{llm-admission:{scope}}}:global"
        user_key = f"csai:{{llm-admission:{scope}}}:user:{user}"
        global_client = client_override or _client
        if global_client is None:
            global_client = redis.Redis.from_url(
                REDIS_URL, decode_responses=False, socket_connect_timeout=2, socket_timeout=2
            )
            globals()["_client"] = global_client
        result = await global_client.eval(
            ADMIT,
            2,
            key,
            user_key,
            rpm,
            rpm / 60,
            user_rpm,
            user_rpm / 60,
            user_tpm,
            user_tpm / 60,
            max(1, estimated_tokens),
            max(1, estimated_tokens),
            user,
            ttl,
            tpm,
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise LLMAdmissionUnavailable("Invalid Redis admission response")
        if int(result[0]) == -1:
            raise LLMAdmissionRejected(None, "request_exceeds_bucket_capacity")
        if int(result[0]) == 0:
            raise LLMAdmissionRejected(int(result[1]))
        if int(result[0]) != 1:
            raise LLMAdmissionUnavailable("Invalid Redis admission decision")
    except (LLMAdmissionRejected, LLMAdmissionUnavailable):
        raise
    except Exception as exc:
        raise LLMAdmissionUnavailable("Shared LLM admission is unavailable") from exc


def estimate_request_tokens(messages: list, max_output_tokens: int) -> int:
    """Conservative text estimate; multimodal payloads use a bounded floor."""
    chars = 0
    for message in messages:
        content = (
            message.get("content", "")
            if isinstance(message, dict)
            else getattr(message, "content", "")
        )
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(len(str(part)) for part in content)
    return max(1, math.ceil(chars / 3) + max(1, max_output_tokens))
