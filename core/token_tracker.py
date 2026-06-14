"""
Token 用量追踪器（v5.1）
记录 LLM 调用的 token 消耗，支持按 agent/session/全局维度统计。

集成方式：在 LLM client 的 async_invoke 返回后调用 record()。
"""

import threading
from collections import defaultdict
from typing import Any

from core.logger import get_logger

logger = get_logger("core.token_tracker")


class TokenUsage:
    """单次 LLM 调用的 token 用量记录"""

    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens", "agent", "model", "latency_ms")

    def __init__(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        agent: str = "",
        model: str = "",
        latency_ms: float = 0.0,
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.agent = agent
        self.model = model
        self.latency_ms = latency_ms


class TokenTracker:
    """Token 用量追踪器：按 agent / 全局维度累计。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 全局计数
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_calls: int = 0
        self.total_latency_ms: float = 0.0
        # 按 agent 维度
        self.agent_tokens: dict[str, int] = defaultdict(int)
        self.agent_calls: dict[str, int] = defaultdict(int)
        self.agent_latency_ms: dict[str, float] = defaultdict(float)
        # 按 model 维度
        self.model_tokens: dict[str, int] = defaultdict(int)
        self.model_calls: dict[str, int] = defaultdict(int)
        # 最近 N 次调用记录（用于延迟分布）
        self._recent_latencies: list[float] = []
        self._max_recent = 1000

    async def record(self, usage: TokenUsage) -> None:
        """记录一次 LLM 调用的 token 用量"""
        with self._lock:
            self.total_prompt_tokens += usage.prompt_tokens
            self.total_completion_tokens += usage.completion_tokens
            self.total_calls += 1
            self.total_latency_ms += usage.latency_ms

            if usage.agent:
                self.agent_tokens[usage.agent] += usage.total_tokens
                self.agent_calls[usage.agent] += 1
                self.agent_latency_ms[usage.agent] += usage.latency_ms

            if usage.model:
                self.model_tokens[usage.model] += usage.total_tokens
                self.model_calls[usage.model] += 1

            # 延迟分布
            if len(self._recent_latencies) >= self._max_recent:
                self._recent_latencies.pop(0)
            self._recent_latencies.append(usage.latency_ms)

    def get_summary(self) -> dict[str, Any]:
        """获取全局 token 用量摘要"""
        avg_latency = (
            self.total_latency_ms / self.total_calls if self.total_calls > 0 else 0.0
        )
        p95_latency = self._percentile(95) if self._recent_latencies else 0.0
        p50_latency = self._percentile(50) if self._recent_latencies else 0.0

        return {
            "total_calls": self.total_calls,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
            "avg_latency_ms": round(avg_latency, 2),
            "p50_latency_ms": round(p50_latency, 2),
            "p95_latency_ms": round(p95_latency, 2),
        }

    def get_agent_summary(self) -> dict[str, dict[str, Any]]:
        """获取按 agent 维度的用量摘要"""
        result = {}
        for agent in set(list(self.agent_tokens.keys()) + list(self.agent_calls.keys())):
            calls = self.agent_calls.get(agent, 0)
            avg_lat = self.agent_latency_ms.get(agent, 0) / calls if calls > 0 else 0
            result[agent] = {
                "calls": calls,
                "total_tokens": self.agent_tokens.get(agent, 0),
                "avg_latency_ms": round(avg_lat, 2),
            }
        return result

    def get_model_summary(self) -> dict[str, dict[str, Any]]:
        """获取按 model 维度的用量摘要"""
        result = {}
        for model in set(list(self.model_tokens.keys()) + list(self.model_calls.keys())):
            result[model] = {
                "calls": self.model_calls.get(model, 0),
                "total_tokens": self.model_tokens.get(model, 0),
            }
        return result

    def _percentile(self, p: int) -> float:
        """计算延迟百分位"""
        if not self._recent_latencies:
            return 0.0
        sorted_lat = sorted(self._recent_latencies)
        idx = int(len(sorted_lat) * p / 100)
        idx = min(idx, len(sorted_lat) - 1)
        return sorted_lat[idx]


# 全局单例
_token_tracker: TokenTracker | None = None


def get_token_tracker() -> TokenTracker | None:
    return _token_tracker


def init_token_tracker() -> TokenTracker:
    global _token_tracker
    if _token_tracker is None:
        _token_tracker = TokenTracker()
    return _token_tracker
