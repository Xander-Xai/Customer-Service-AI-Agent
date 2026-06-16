"""
Token Quota 管理器（v5.3）
基于用户账户的 Token 消耗硬性上限控制。

功能：
- 检查用户是否超出每日/每月 Token 配额
- 记录 LLM 调用消耗的 Token
- 自动按日/月重置配额
- 支持 Redis（生产）和内存（开发/测试）存储

集成方式：
1. 在 LLM client 调用前 check_quota(user_id)
2. 在 LLM 返回后 consume_tokens(user_id, tokens)
3. 配额不足时返回降级响应
"""

import threading
import time
from dataclasses import dataclass
from typing import Any

from core.config import (
    REDIS_URL,
    TOKEN_QUOTA_DAILY,
    TOKEN_QUOTA_ENABLED,
    TOKEN_QUOTA_MONTHLY,
    TOKEN_QUOTA_REDIS_PREFIX,
)
from core.logger import get_logger

logger = get_logger("core.token_quota")


@dataclass
class UserQuota:
    """单个用户的配额状态"""

    user_id: str
    used_today: int = 0
    used_this_month: int = 0
    last_reset_day: int = 0  # 上次日重置的 day_of_year
    last_reset_month: int = 0  # 上次月重置的 month

    @property
    def daily_remaining(self) -> int:
        return max(0, TOKEN_QUOTA_DAILY - self.used_today)

    @property
    def monthly_remaining(self) -> int:
        return max(0, TOKEN_QUOTA_MONTHLY - self.used_this_month)

    def reset_if_needed(self) -> bool:
        """检查并执行日/月重置，返回是否发生了重置"""
        now = time.localtime()
        day = now.tm_yday
        month = now.tm_mon
        reset = False
        if self.last_reset_day != day:
            self.used_today = 0
            self.last_reset_day = day
            reset = True
        if self.last_reset_month != month:
            self.used_this_month = 0
            self.last_reset_month = month
            reset = True
        return reset

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "used_today": self.used_today,
            "used_this_month": self.used_this_month,
            "last_reset_day": self.last_reset_day,
            "last_reset_month": self.last_reset_month,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserQuota":
        return cls(
            user_id=data["user_id"],
            used_today=data["used_today"],
            used_this_month=data["used_this_month"],
            last_reset_day=data["last_reset_day"],
            last_reset_month=data["last_reset_month"],
        )


class _QuotaBackend:
    """Quota 存储后端抽象（内存 / Redis）"""

    def get(self, user_id: str) -> UserQuota | None:
        raise NotImplementedError

    def set(self, user_id: str, quota: UserQuota) -> None:
        raise NotImplementedError

    def get_all(self) -> dict[str, UserQuota]:
        raise NotImplementedError


class _MemoryBackend(_QuotaBackend):
    """内存存储后端（开发/测试）"""

    def __init__(self) -> None:
        self._quotas: dict[str, UserQuota] = {}
        self._lock = threading.Lock()

    def get(self, user_id: str) -> UserQuota | None:
        with self._lock:
            return self._quotas.get(user_id)

    def set(self, user_id: str, quota: UserQuota) -> None:
        with self._lock:
            self._quotas[user_id] = quota

    def get_all(self) -> dict[str, UserQuota]:
        with self._lock:
            return dict(self._quotas)


class _RedisBackend(_QuotaBackend):
    """Redis 存储后端（生产）"""

    def __init__(self, redis_url: str, prefix: str) -> None:
        import redis

        self._client = redis.from_url(redis_url, decode_responses=True)
        self._prefix = prefix
        self._lock = threading.Lock()

    def _key(self, user_id: str) -> str:
        return f"{self._prefix}{user_id}"

    def get(self, user_id: str) -> UserQuota | None:
        try:
            data = self._client.hgetall(self._key(user_id))
            if not data:
                return None
            return UserQuota.from_dict({k: int(v) for k, v in data.items()})
        except Exception as e:
            logger.warning(f"[TokenQuota] Redis get failed: {e}, fallback to memory")
            return None

    def set(self, user_id: str, quota: UserQuota) -> None:
        try:
            self._client.hset(self._key(user_id), mapping=quota.to_dict())
            # TTL: 90 天（覆盖月重置周期）
            self._client.expire(self._key(user_id), 90 * 24 * 3600)
        except Exception as e:
            logger.warning(f"[TokenQuota] Redis set failed: {e}")

    def get_all(self) -> dict[str, UserQuota]:
        try:
            result = {}
            for key in self._client.scan_iter(match=f"{self._prefix}*"):
                user_id = key[len(self._prefix) :]
                data = self._client.hgetall(key)
                if data:
                    result[user_id] = UserQuota.from_dict({k: int(v) for k, v in data.items()})
            return result
        except Exception as e:
            logger.warning(f"[TokenQuota] Redis get_all failed: {e}")
            return {}


class TokenQuotaManager:
    """Token Quota 管理器：按用户账户控制 Token 消耗上限"""

    def __init__(self, backend: _QuotaBackend | None = None) -> None:
        self._enabled = TOKEN_QUOTA_ENABLED
        # 自动选择后端：Redis 可用则用 Redis，否则内存
        if backend is None:
            try:
                import redis

                redis.from_url(REDIS_URL).ping()
                backend = _RedisBackend(REDIS_URL, TOKEN_QUOTA_REDIS_PREFIX)
                logger.info("[TokenQuota] Using Redis backend")
            except Exception:
                backend = _MemoryBackend()
                logger.info("[TokenQuota] Using memory backend")
        self._backend = backend

    def _get_or_create_quota(self, user_id: str) -> UserQuota:
        """获取或创建用户配额记录"""
        quota = self._backend.get(user_id)
        if quota is None:
            quota = UserQuota(user_id=user_id)
            self._backend.set(user_id, quota)
        return quota

    def check_quota(self, user_id: str) -> dict[str, Any]:
        """
        检查用户是否还有可用配额

        返回: {"allowed": bool, "daily_remaining": int, "monthly_remaining": int, "reason": str}
        """
        if not self._enabled:
            return {
                "allowed": True,
                "daily_remaining": TOKEN_QUOTA_DAILY,
                "monthly_remaining": TOKEN_QUOTA_MONTHLY,
                "reason": "",
            }

        quota = self._get_or_create_quota(user_id)
        quota.reset_if_needed()
        self._backend.set(user_id, quota)

        if quota.daily_remaining <= 0:
            return {
                "allowed": False,
                "daily_remaining": 0,
                "monthly_remaining": quota.monthly_remaining,
                "reason": f"今日 Token 配额已用尽（上限 {TOKEN_QUOTA_DAILY}），请明日再试",
            }
        if quota.monthly_remaining <= 0:
            return {
                "allowed": False,
                "daily_remaining": quota.daily_remaining,
                "monthly_remaining": 0,
                "reason": f"本月 Token 配额已用尽（上限 {TOKEN_QUOTA_MONTHLY}），请下月再试",
            }

        return {
            "allowed": True,
            "daily_remaining": quota.daily_remaining,
            "monthly_remaining": quota.monthly_remaining,
            "reason": "",
        }

    def consume_tokens(self, user_id: str, tokens: int) -> dict[str, Any]:
        """
        消耗用户的 Token 配额

        返回: {"success": bool, "daily_remaining": int, "monthly_remaining": int, "reason": str}
        """
        if not self._enabled:
            return {
                "success": True,
                "daily_remaining": TOKEN_QUOTA_DAILY,
                "monthly_remaining": TOKEN_QUOTA_MONTHLY,
                "reason": "",
            }

        if tokens <= 0:
            return {"success": True, "daily_remaining": 0, "monthly_remaining": 0, "reason": ""}

        quota = self._get_or_create_quota(user_id)
        quota.reset_if_needed()

        if quota.daily_remaining < tokens:
            return {
                "success": False,
                "daily_remaining": quota.daily_remaining,
                "monthly_remaining": quota.monthly_remaining,
                "reason": f"本次请求需 {tokens} Token，但今日仅剩 {quota.daily_remaining}",
            }
        if quota.monthly_remaining < tokens:
            return {
                "success": False,
                "daily_remaining": quota.daily_remaining,
                "monthly_remaining": quota.monthly_remaining,
                "reason": f"本次请求需 {tokens} Token，但本月仅剩 {quota.monthly_remaining}",
            }

        quota.used_today += tokens
        quota.used_this_month += tokens
        self._backend.set(user_id, quota)

        logger.info(
            f"[TokenQuota] user={user_id} consumed={tokens} "
            f"daily={quota.used_today}/{TOKEN_QUOTA_DAILY} "
            f"monthly={quota.used_this_month}/{TOKEN_QUOTA_MONTHLY}"
        )

        return {
            "success": True,
            "daily_remaining": quota.daily_remaining,
            "monthly_remaining": quota.monthly_remaining,
            "reason": "",
        }

    def get_quota_status(self, user_id: str) -> dict[str, Any]:
        """获取用户配额状态"""
        quota = self._get_or_create_quota(user_id)
        quota.reset_if_needed()
        self._backend.set(user_id, quota)
        return {
            "user_id": user_id,
            "daily_used": quota.used_today,
            "daily_limit": TOKEN_QUOTA_DAILY,
            "daily_remaining": quota.daily_remaining,
            "monthly_used": quota.used_this_month,
            "monthly_limit": TOKEN_QUOTA_MONTHLY,
            "monthly_remaining": quota.monthly_remaining,
        }

    def get_all_status(self) -> dict[str, dict[str, Any]]:
        """获取所有用户的配额状态"""
        result = {}
        for user_id, quota in self._backend.get_all().items():
            quota.reset_if_needed()
            self._backend.set(user_id, quota)
            result[user_id] = {
                "daily_used": quota.used_today,
                "daily_limit": TOKEN_QUOTA_DAILY,
                "daily_remaining": quota.daily_remaining,
                "monthly_used": quota.used_this_month,
                "monthly_limit": TOKEN_QUOTA_MONTHLY,
                "monthly_remaining": quota.monthly_remaining,
            }
        return result


# 全局单例
_quota_manager: TokenQuotaManager | None = None


def get_quota_manager() -> TokenQuotaManager:
    """获取 Token Quota 管理器单例"""
    global _quota_manager
    if _quota_manager is None:
        _quota_manager = TokenQuotaManager()
    return _quota_manager
