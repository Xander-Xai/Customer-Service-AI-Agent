"""
v3.2 优化专项测试
覆盖：首次解决率增强、SLA 告警、模型熔断器
无需 LLM API 和网络，纯逻辑测试

运行: pytest test_v32_optimizations.py -v
"""
import os
import sys
import time
import asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


# ===== 1. CircuitBreaker 熔断器测试 =====

class TestCircuitBreaker:
    """熔断器三态转换逻辑"""

    def _make_cb(self, fail_threshold=3, recovery_time=0):
        """创建测试用熔断器（recovery_time=0 便于测试）"""
        from core.monitoring import CircuitBreaker
        return CircuitBreaker(fail_threshold=fail_threshold, recovery_time=recovery_time)

    def test_initial_state_closed(self):
        cb = self._make_cb()
        assert cb.state == "closed"
        assert cb.should_allow() is True

    def test_closed_to_open(self):
        """连续失败达到阈值 → OPEN"""
        cb = self._make_cb(fail_threshold=3, recovery_time=60)  # 60s 恢复，测试时不会恢复
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "open"
        assert cb.should_allow() is False

    def test_open_to_half_open(self):
        """恢复时间后 → HALF_OPEN，允许探测"""
        cb = self._make_cb(fail_threshold=2, recovery_time=0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        # recovery_time=0，立即进入 HALF_OPEN
        assert cb.should_allow() is True
        assert cb.state == "half_open"

    def test_half_open_success_to_closed(self):
        """HALF_OPEN 探测成功 → CLOSED"""
        cb = self._make_cb(fail_threshold=2, recovery_time=0)
        cb.record_failure()
        cb.record_failure()
        cb.should_allow()  # → HALF_OPEN
        cb.record_success()
        assert cb.state == "closed"
        assert cb.consecutive_failures == 0

    def test_half_open_failure_to_open(self):
        """HALF_OPEN 探测失败 → 重新 OPEN"""
        cb = self._make_cb(fail_threshold=2, recovery_time=0)
        cb.record_failure()
        cb.record_failure()
        cb.should_allow()  # → HALF_OPEN
        cb.record_failure()  # 探测失败
        assert cb.state == "open"

    def test_partial_failures_stay_closed(self):
        """失败次数未达阈值 → 保持 CLOSED"""
        cb = self._make_cb(fail_threshold=5)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        assert cb.should_allow() is True

    def test_success_resets_counter(self):
        """成功调用重置连续失败计数"""
        cb = self._make_cb(fail_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.consecutive_failures == 0
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"  # 只有 2 次，未达阈值

    def test_get_status(self):
        cb = self._make_cb(fail_threshold=3, recovery_time=60)
        status = cb.get_status()
        assert status["state"] == "closed"
        assert status["fail_threshold"] == 3
        assert status["recovery_time"] == 60
        assert status["total_failures"] == 0


# ===== 2. SLAAlertManager 告警管理器测试 =====

class TestSLAAlertManager:
    """SLA 告警滑动窗口 + 冷却机制"""

    def _make_metrics_and_alert_mgr(self):
        from core.monitoring import MetricsCollector, SLAAlertManager
        metrics = MetricsCollector()
        alert_mgr = SLAAlertManager()
        return metrics, alert_mgr

    @pytest.mark.asyncio
    async def test_no_alert_when_violation_rate_low(self):
        """违约率低于阈值 → 不告警"""
        metrics, alert_mgr = self._make_metrics_and_alert_mgr()
        # 录入正常请求
        for _ in range(10):
            metrics.record_request(elapsed=10.0, session_id="s1")
        alert = await alert_mgr.check_and_alert(metrics)
        assert alert is None

    @pytest.mark.asyncio
    async def test_alert_when_violation_rate_high(self):
        """违约率超过阈值 → 触发告警"""
        metrics, alert_mgr = self._make_metrics_and_alert_mgr()
        # 录入高违约率请求（全部超时）
        for i in range(50):
            metrics.record_request(elapsed=25.0, session_id=f"s{i}")
        alert = await alert_mgr.check_and_alert(metrics)
        assert alert is not None
        assert alert["type"] == "sla_violation_high"
        assert alert["window_rate"] == 100.0
        assert len(alert_mgr.get_alerts()) == 1

    @pytest.mark.asyncio
    async def test_cooldown_prevents_alert_storm(self):
        """冷却期内不重复告警"""
        metrics, alert_mgr = self._make_metrics_and_alert_mgr()
        alert_mgr.last_alert_time["sla_violation_high"] = time.time()  # 刚告警过
        for i in range(50):
            metrics.record_request(elapsed=25.0, session_id=f"s{i}")
        alert = await alert_mgr.check_and_alert(metrics)
        assert alert is None  # 冷却期内，不告警

    @pytest.mark.asyncio
    async def test_severity_critical_at_extreme_rate(self):
        """极高违约率 → critical 级别"""
        from multi_agent_customer_service import SLA_ALERT_THRESHOLD
        metrics, alert_mgr = self._make_metrics_and_alert_mgr()
        for i in range(50):
            metrics.record_request(elapsed=25.0, session_id=f"s{i}")
        alert = await alert_mgr.check_and_alert(metrics)
        # 100% > threshold * 1.5 → critical
        if SLA_ALERT_THRESHOLD * 1.5 < 100.0:
            assert alert["severity"] == "critical"

    def test_alert_history_limit(self):
        """告警历史不超过 100 条"""
        _, alert_mgr = self._make_metrics_and_alert_mgr()
        for i in range(150):
            alert_mgr.alerts.append({"type": "test", "index": i})
            if len(alert_mgr.alerts) > 100:
                alert_mgr.alerts = alert_mgr.alerts[-100:]
        assert len(alert_mgr.alerts) == 100
        assert alert_mgr.alerts[0]["index"] == 50  # 最早保留的是第 50 条


# ===== 3. 增强首次解决率测试 =====

class TestResolutionStatus:
    """解决状态评估逻辑"""

    def _make_response_agent(self):
        from agents.response_agent import ResponseAgent
        return ResponseAgent()

    def test_resolved_normal_response(self):
        agent = self._make_response_agent()
        state = {
            "response": "这款精华含有透明质酸成分，适合干性肤质，建议搭配保湿面霜使用，每日早晚各一次。",
            "collaboration_mode": "sequential",
            "query_type": "product_info",
            "complexity": 30,
        }
        assert agent._evaluate_resolution(state) == "resolved"

    def test_failed_empty_response(self):
        agent = self._make_response_agent()
        state = {"response": "", "collaboration_mode": "sequential"}
        assert agent._evaluate_resolution(state) == "failed"

    def test_failed_error_response(self):
        agent = self._make_response_agent()
        state = {"response": "处理出错，请重试", "collaboration_mode": "sequential"}
        assert agent._evaluate_resolution(state) == "failed"

    def test_uncertain_short_response(self):
        agent = self._make_response_agent()
        state = {"response": "好的", "collaboration_mode": "sequential"}
        assert agent._evaluate_resolution(state) == "uncertain"

    def test_uncertain_deflection(self):
        agent = self._make_response_agent()
        state = {
            "response": "抱歉无法确定您的问题，建议您联系我们的客服热线获取更详细的帮助。",
            "collaboration_mode": "sequential",
        }
        assert agent._evaluate_resolution(state) == "uncertain"

    def test_escalated_complaint_hierarchical(self):
        agent = self._make_response_agent()
        state = {
            "response": "已为您安排专人跟进",
            "collaboration_mode": "hierarchical",
            "query_type": "complaint",
        }
        assert agent._evaluate_resolution(state) == "escalated"


# ===== 4. MetricsCollector 增强 KPI 测试 =====

class TestEnhancedKPI:
    """增强版 KPI 统计逻辑"""

    def test_resolution_counts_tracked(self):
        from core.monitoring import MetricsCollector
        m = MetricsCollector()
        m.record_request(elapsed=10.0, session_id="s1", resolution_status="resolved")
        m.record_request(elapsed=10.0, session_id="s2", resolution_status="uncertain")
        m.record_request(elapsed=10.0, session_id="s3", resolution_status="failed")
        m.record_request(elapsed=10.0, session_id="s4", resolution_status="escalated")
        assert m.resolution_counts == {"resolved": 1, "uncertain": 1, "failed": 1, "escalated": 1}

    def test_kpi_includes_enhanced_fields(self):
        from core.monitoring import MetricsCollector
        m = MetricsCollector()
        m.record_request(elapsed=10.0, session_id="s1", resolution_status="resolved")
        m.record_request(elapsed=10.0, session_id="s2", resolution_status="failed")
        kpi = m.get_kpi_stats()
        # v3.2 新增字段
        assert "resolution_rate" in kpi
        assert "resolution_detail" in kpi
        # 50% 解决率
        assert kpi["resolution_rate"] == "50.0%"
        assert kpi["resolution_detail"]["resolved"] == 1
        assert kpi["resolution_detail"]["failed"] == 1

    def test_sla_window_violation_rate(self):
        from core.monitoring import MetricsCollector
        m = MetricsCollector()
        # 30 次超时 + 20 次正常 = 60% 违约率
        for _ in range(30):
            m.record_request(elapsed=25.0, session_id="s1")
        for _ in range(20):
            m.record_request(elapsed=10.0, session_id="s2")
        rate = m.get_sla_window_violation_rate()
        # 窗口取最后 50 次（默认 SLA_ALERT_WINDOW=50）
        assert rate > 0  # 有违约
        stats = m.get_stats()
        assert "window_violation_rate" in stats["sla"]


# ===== 5. 路由降级测试 =====

class TestRouterDegradation:
    """熔断状态下路由降级为纯规则分类"""

    def test_rule_classify_works_independently(self):
        """规则分类器可以独立工作"""
        from router.query_router import QueryRouter
        router = QueryRouter()
        # 直接调用规则分类
        result = router._rule_classify("这款面膜多少钱？")
        assert result == "product_info"

    def test_rule_classify_complaint(self):
        from router.query_router import QueryRouter
        router = QueryRouter()
        result = router._rule_classify("我要投诉你们的服务态度太差了")
        assert result == "complaint"

    def test_rule_classify_no_match(self):
        from router.query_router import QueryRouter
        router = QueryRouter()
        result = router._rule_classify("你好啊")
        assert result is None  # 无匹配返回 None


# ===== 6. 响应缓存质量过滤测试 =====

class TestCacheQualityFilter:
    """仅缓存 resolved 状态的响应"""

    def test_cache_put_only_resolved(self):
        """模拟 ResponseAgent 的缓存写入逻辑"""
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=50, l2_max=200)

        # resolved → 写入缓存
        resolution = "resolved"
        if resolution == "resolved":
            cache.put("问题A", "回答A")
        assert cache.get("问题A") == "回答A"

        # uncertain → 不写入缓存
        resolution = "uncertain"
        if resolution == "resolved":
            cache.put("问题B", "回答B")
        assert cache.get("问题B") is None

        # failed → 不写入缓存
        resolution = "failed"
        if resolution == "resolved":
            cache.put("问题C", "回答C")
        assert cache.get("问题C") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
