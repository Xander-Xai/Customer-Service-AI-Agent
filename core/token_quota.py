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

import asyncio
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from core.config import (
    TOKEN_QUOTA_DAILY,
    TOKEN_QUOTA_ENABLED,
    TOKEN_QUOTA_MONTHLY,
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


class TokenQuotaManager:
    """Token Quota 管理器：按用户账户控制 Token 消耗上限"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quotas: dict[str, UserQuota] = {}
        self._enabled = TOKEN_QUOTA_ENABLED

    def _get_or_create_quota(self, user_id: str) -> UserQuota:
        """获取或创建用户配额记录"""
        quota = self._quotas.get(user_id)
        if quota is None:
            quota = UserQuota(user_id=user_id)
            self._quotas[user_id] = quota
        return quota

    def check_quota(self, user_id: str) -> dict[str, Any]:
        """
        检查用户是否还有可用配额

        返回: {"allowed": bool, "daily_remaining": int, "monthly_remaining": int, "reason": str}
        """
        if not self._enabled:
            return {"allowed": True, "daily_remaining": TOKEN_QUOTA_DAILY, "monthly_remaining": TOKEN_QUOTA_MONTHLY, "reason": ""}

        with self._lock:
            quota = self._get_or_create_quota(user_id)
            quota.reset_if_needed()

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
            return {"success": True, "daily_remaining": TOKEN_QUOTA_DAILY, "monthly_remaining": TOKEN_QUOTA_MONTHLY, "reason": ""}

        if tokens <= 0:
            return {"success": True, "daily_remaining": 0, "monthly_remaining": 0, "reason": ""}

        with self._lock:
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
        with self._lock:
            quota = self._get_or_create_quota(user_id)
            quota.reset_if_needed()
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
        with self._lock:
            result = {}
            for user_id, quota in self._quotas.items():
                quota.reset_if_needed()
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
