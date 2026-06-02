"""
监控与基础设施模块（从 multi_agent_customer_service.py 提取）
- MetricsCollector: 性能指标实时采集（KPI + SLA + 解决率）
- CircuitBreaker: LLM 调用熔断器（三态：CLOSED / OPEN / HALF_OPEN）
- SLAAlertManager: SLA 违约率滑动窗口告警
- OpenAICompatibleClient: OpenAI 兼容异步 HTTP 客户端（指数退避 + 熔断器）
- CustomResponse: LLM 响应包装
"""
import time
import json
import random
import asyncio
import httpx
from typing import Dict, List, Any, Optional

from logger import get_logger
from config import (
    RESPONSE_TIME_TARGET_MAX, RESPONSE_TIME_TARGET_MIN,
    SLA_ALERT_WINDOW, SLA_ALERT_THRESHOLD, SLA_ALERT_COOLDOWN,
    CIRCUIT_BREAKER_FAIL_THRESHOLD, CIRCUIT_BREAKER_RECOVERY_TIME,
    RETRY_MAX_ATTEMPTS, RETRY_BASE_DELAY, HTTP_TIMEOUT, HTTP_HEADERS,
    HTTPX_MAX_CONNECTIONS, HTTPX_KEEPALIVE_CONNECTIONS,
)

logger = get_logger("monitoring")


# ===== 性能指标采集器 =====
class MetricsCollector:
    """系统性能指标实时采集（v3.2: 增强首次解决率 + SLA 告警）"""

    def __init__(self):
        self.total_requests = 0
        self.total_errors = 0
        self.response_times: List[float] = []  # 仅保留最近 STAT_WINDOW 条
        self.agent_call_counts: Dict[str, int] = {}
        self.mode_counts: Dict[str, int] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        # SLA 追踪
        self.sla_violations: int = 0
        self.sla_too_fast: int = 0
        # Business KPI tracking（v3.2: 增强版）
        self.total_single_turn_resolved = 0
        self.total_ai_handled = 0
        self.total_escalated = 0
        self.session_turn_counts: Dict[str, int] = {}
        self.session_last_activity: Dict[str, float] = {}
        self._session_ttl: float = 3600.0  # 会话统计 1 小时过期
        self._last_cleanup: float = time.time()
        # v3.2: 细粒度解决率追踪
        self.resolution_counts: Dict[str, int] = {
            "resolved": 0, "uncertain": 0, "failed": 0, "escalated": 0,
        }
        # v3.2: SLA 告警滑动窗口
        self._sla_window: List[float] = []

    def record_request(self, elapsed: float, agent: str = "", mode: str = "", cached: bool = False,
                       error: bool = False, session_id: str = None, escalated: bool = False,
                       resolution_status: str = ""):
        self.total_requests += 1
        if error:
            self.total_errors += 1
        if cached:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        # 仅保留最近 200 条，防止内存无限增长
        self.response_times.append(elapsed)
        if len(self.response_times) > 200:
            self.response_times = self.response_times[-200:]
        # SLA 追踪
        if elapsed > RESPONSE_TIME_TARGET_MAX:
            self.sla_violations += 1
        if elapsed < RESPONSE_TIME_TARGET_MIN:
            self.sla_too_fast += 1
        # v3.2: SLA 滑动窗口
        self._sla_window.append(elapsed)
        if len(self._sla_window) > SLA_ALERT_WINDOW:
            self._sla_window = self._sla_window[-SLA_ALERT_WINDOW:]
        if agent:
            self.agent_call_counts[agent] = self.agent_call_counts.get(agent, 0) + 1
        if mode:
            self.mode_counts[mode] = self.mode_counts.get(mode, 0) + 1

        # Business KPI tracking（v3.2: 细粒度解决率）
        now = time.time()
        if session_id is not None:
            is_first_turn = session_id not in self.session_turn_counts
            self.session_turn_counts[session_id] = self.session_turn_counts.get(session_id, 0) + 1
            self.session_last_activity[session_id] = now
            if is_first_turn and not escalated and not cached:
                self.total_single_turn_resolved += 1
            # 定期清理过期会话统计（每 100 次请求清理一次）
            if self.total_requests % 100 == 0:
                self._cleanup_expired_sessions(now)
        if escalated:
            self.total_escalated += 1
        else:
            self.total_ai_handled += 1
        # v3.2: 细粒度解决状态统计
        if resolution_status and resolution_status in self.resolution_counts:
            self.resolution_counts[resolution_status] += 1

    def _cleanup_expired_sessions(self, now: float):
        """清理过期的会话统计，防止内存无限增长"""
        expired = [sid for sid, ts in self.session_last_activity.items()
                   if now - ts > self._session_ttl]
        for sid in expired:
            self.session_turn_counts.pop(sid, None)
            self.session_last_activity.pop(sid, None)
        if expired:
            logger.debug(f"[Metrics] 清理 {len(expired)} 个过期会话统计")

    def get_stats(self) -> Dict[str, Any]:
        times = self.response_times[-100:]  # 最近 100 次
        return {
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "error_rate": round(self.total_errors / max(self.total_requests, 1) * 100, 1),
            "avg_response_time": round(sum(times) / max(len(times), 1), 2),
            "p95_response_time": round(sorted(times)[int(len(times) * 0.95)] if len(times) >= 20 else (max(times) if times else 0), 2),
            "cache_hit_rate": round(self.cache_hits / max(self.cache_hits + self.cache_misses, 1) * 100, 1),
            "agent_call_counts": dict(self.agent_call_counts),
            "mode_counts": dict(self.mode_counts),
            "sla": {
                "target_min": RESPONSE_TIME_TARGET_MIN,
                "target_max": RESPONSE_TIME_TARGET_MAX,
                "violations_slow": self.sla_violations,
                "violations_fast": self.sla_too_fast,
                "violation_rate": round(self.sla_violations / max(self.total_requests, 1) * 100, 1),
                "window_violation_rate": self.get_sla_window_violation_rate(),
            },
        }

    def get_sla_window_violation_rate(self) -> float:
        """v3.2: 计算滑动窗口内的 SLA 违约率"""
        if not self._sla_window:
            return 0.0
        violations = sum(1 for t in self._sla_window if t > RESPONSE_TIME_TARGET_MAX)
        return round(violations / len(self._sla_window) * 100, 1)

    def get_kpi_stats(self) -> Dict[str, Any]:
        total_sessions = len(self.session_turn_counts)
        single_turn_sessions = sum(1 for v in self.session_turn_counts.values() if v == 1)
        first_resolution_rate = single_turn_sessions / max(total_sessions, 1) * 100
        ai_handled_rate = self.total_ai_handled / max(self.total_requests, 1) * 100

        # v3.2 口径：基于 Agent 信号的解决率（更准确）
        total_resolution = sum(self.resolution_counts.values())
        resolved = self.resolution_counts.get("resolved", 0)
        resolution_rate = resolved / max(total_resolution, 1) * 100

        return {
            "first_resolution_rate": f"{first_resolution_rate:.1f}%",
            "first_resolution_detail": f"{single_turn_sessions}/{total_sessions}",
            # v3.2 增强指标
            "resolution_rate": f"{resolution_rate:.1f}%",
            "resolution_detail": {
                "resolved": resolved,
                "uncertain": self.resolution_counts.get("uncertain", 0),
                "failed": self.resolution_counts.get("failed", 0),
                "escalated": self.resolution_counts.get("escalated", 0),
                "total": total_resolution,
            },
            "ai_handled_rate": f"{ai_handled_rate:.1f}%",
            "labor_savings_estimate": f"{ai_handled_rate:.1f}%",
            "total_ai_handled": self.total_ai_handled,
            "total_escalated": self.total_escalated,
            "total_single_turn_resolved": single_turn_sessions,
            "total_multi_turn": total_sessions - single_turn_sessions,
        }

    def save_snapshot(self, redis_client=None) -> bool:
        """持久化指标快照到 Redis（可选，需 Redis 可用）"""
        if redis_client is None:
            return False
        try:
            snapshot = {
                "timestamp": time.time(),
                "stats": self.get_stats(),
                "kpi": self.get_kpi_stats(),
            }
            redis_client.setex("metrics:snapshot", 86400, json.dumps(snapshot, ensure_ascii=False))
            redis_client.lpush("metrics:history", json.dumps(snapshot, ensure_ascii=False))
            redis_client.ltrim("metrics:history", 0, 23)
            return True
        except Exception as e:
            logger.warning(f"[Metrics] 持久化快照失败: {e}")
            return False

    @staticmethod
    def load_snapshot(redis_client=None) -> Optional[Dict[str, Any]]:
        """从 Redis 加载最近的指标快照"""
        if redis_client is None:
            return None
        try:
            data = redis_client.get("metrics:snapshot")
            return json.loads(data) if data else None
        except Exception:
            return None


# ===== 模型熔断器 =====
class CircuitBreaker:
    """
    LLM 调用熔断器：连续失败达到阈值后进入 OPEN 状态，
    跳过 LLM 调用直接走降级路径（规则分类），恢复时间后进入 HALF_OPEN 尝试探测。
    三态：CLOSED（正常）→ OPEN（熔断）→ HALF_OPEN（探测）→ CLOSED
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, fail_threshold: int = None, recovery_time: int = None):
        self.fail_threshold = fail_threshold if fail_threshold is not None else CIRCUIT_BREAKER_FAIL_THRESHOLD
        self.recovery_time = recovery_time if recovery_time is not None else CIRCUIT_BREAKER_RECOVERY_TIME
        self.state = self.CLOSED
        self.consecutive_failures = 0
        self.last_failure_time = 0.0
        self.total_failures = 0
        self.total_successes = 0

    def record_success(self):
        self.consecutive_failures = 0
        self.total_successes += 1
        if self.state == self.HALF_OPEN:
            self.state = self.CLOSED
            logger.info("[CircuitBreaker] HALF_OPEN → CLOSED，LLM 恢复正常")

    def record_failure(self):
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_failure_time = time.time()
        if self.state == self.HALF_OPEN:
            self.state = self.OPEN
            logger.warning(f"[CircuitBreaker] HALF_OPEN → OPEN，探测失败，重新熔断 {self.recovery_time}s")
        elif self.consecutive_failures >= self.fail_threshold and self.state == self.CLOSED:
            self.state = self.OPEN
            logger.warning(
                f"[CircuitBreaker] CLOSED → OPEN，连续 {self.consecutive_failures} 次 LLM 失败，"
                f"熔断 {self.recovery_time}s，后续走降级路径"
            )

    def should_allow(self) -> bool:
        if self.state == self.CLOSED:
            return True
        if self.state == self.OPEN:
            elapsed = time.time() - self.last_failure_time
            if elapsed >= self.recovery_time:
                self.state = self.HALF_OPEN
                logger.info("[CircuitBreaker] OPEN → HALF_OPEN，尝试探测 LLM")
                return True
            return False
        # HALF_OPEN: 允许一次探测
        return True

    def get_status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "fail_threshold": self.fail_threshold,
            "recovery_time": self.recovery_time,
        }


# ===== SLA 告警管理器 =====
class SLAAlertManager:
    """
    SLA 告警管理器：基于滑动窗口检测违约率，
    超过阈值时通过 MessageBus 发布告警，并支持冷却机制避免告警风暴。
    """

    ALERT_HISTORY_MAX = 100  # 告警历史保留上限

    def __init__(self, bus=None):
        self.bus = bus
        self.alerts: List[Dict[str, Any]] = []
        self.last_alert_time: Dict[str, float] = {}

    async def check_and_alert(self, metrics: MetricsCollector) -> Optional[Dict[str, Any]]:
        window_rate = metrics.get_sla_window_violation_rate()
        alert_key = "sla_violation_high"

        if window_rate > SLA_ALERT_THRESHOLD:
            last_time = self.last_alert_time.get(alert_key, 0)
            if time.time() - last_time < SLA_ALERT_COOLDOWN:
                return None

            alert = {
                "type": alert_key,
                "severity": "warning" if window_rate < SLA_ALERT_THRESHOLD * 1.5 else "critical",
                "message": f"SLA 违约率 {window_rate}% 超过阈值 {SLA_ALERT_THRESHOLD}%（最近 {SLA_ALERT_WINDOW} 次请求）",
                "window_rate": window_rate,
                "threshold": SLA_ALERT_THRESHOLD,
                "window_size": len(metrics._sla_window),
                "timestamp": time.time(),
            }

            self.alerts.append(alert)
            if len(self.alerts) > self.ALERT_HISTORY_MAX:
                self.alerts = self.alerts[-self.ALERT_HISTORY_MAX:]
            self.last_alert_time[alert_key] = time.time()

            logger.warning(f"[SLA-Alert] {alert['message']} (severity={alert['severity']})")

            if self.bus:
                try:
                    from core.message_bus import Message, MessageType
                    await self.bus.publish(Message(
                        msg_type=MessageType.BROADCAST,
                        topic="alert.sla",
                        sender="sla_alert_manager",
                        payload=alert,
                    ))
                except Exception:
                    pass

            return alert
        return None

    def get_alerts(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.alerts[-limit:]


# ===== OpenAI 兼容客户端 =====
class CustomResponse:
    """LLM 响应包装"""
    def __init__(self, content: str):
        self.content = content


class OpenAICompatibleClient:
    """OpenAI 兼容异步客户端（v3.2: 熔断器 + 分层超时）"""
    _async_client = None

    def __init__(self, api_key: str, base_url: str, model: str,
                 circuit_breaker: Optional[CircuitBreaker] = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = HTTP_TIMEOUT
        self.max_retries = RETRY_MAX_ATTEMPTS
        self.base_delay = RETRY_BASE_DELAY
        self.headers = HTTP_HEADERS.copy()
        self.headers["Authorization"] = f"Bearer {api_key}"
        self.circuit_breaker = circuit_breaker

    @staticmethod
    def _format_messages(messages) -> list:
        """统一消息格式化"""
        role_map = {"human": "user", "ai": "assistant", "system": "system"}
        formatted = []
        for msg in messages:
            if hasattr(msg, 'content'):
                role = role_map.get(getattr(msg, 'type', 'user'), "user")
                formatted.append({"role": role, "content": msg.content})
            else:
                formatted.append({"role": "user", "content": str(msg)})
        return formatted

    async def _get_async_client(self) -> httpx.AsyncClient:
        """懒初始化模块级 httpx.AsyncClient 单例（连接池复用）"""
        if OpenAICompatibleClient._async_client is None or OpenAICompatibleClient._async_client.is_closed:
            OpenAICompatibleClient._async_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                limits=httpx.Limits(
                    max_connections=HTTPX_MAX_CONNECTIONS,
                    max_keepalive_connections=HTTPX_KEEPALIVE_CONNECTIONS,
                ),
            )
        return OpenAICompatibleClient._async_client

    async def async_invoke(self, messages, timeout: Optional[float] = None):
        """原生异步调用（v3.2: 支持分层超时 + 熔断器）"""
        if self.circuit_breaker and not self.circuit_breaker.should_allow():
            raise Exception("CircuitBreaker OPEN: LLM 调用已熔断，走降级路径")

        payload = {"model": self.model, "messages": self._format_messages(messages)}
        client = await self._get_async_client()
        call_timeout = httpx.Timeout(timeout or self.timeout)

        last_error = None
        for attempt in range(self.max_retries):
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload, headers=self.headers, timeout=call_timeout,
                )
                resp.raise_for_status()
                result = resp.json()
                if "choices" in result and result["choices"]:
                    content = result["choices"][0].get("message", {}).get("content", "")
                    if self.circuit_breaker:
                        self.circuit_breaker.record_success()
                    return CustomResponse(content)
                return CustomResponse("API response format error")
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                last_error = e
                if attempt == self.max_retries - 1:
                    break
                delay = self.base_delay * (2 ** attempt) + random.uniform(0, 0.3)
                logger.warning(f"[LLM-async] retry {attempt+1}/{self.max_retries}: {e}, wait {delay:.1f}s")
                await asyncio.sleep(delay)

        if self.circuit_breaker:
            self.circuit_breaker.record_failure()
        raise Exception(f"API call failed after {self.max_retries} attempts: {last_error}")
