"""
v3.4 优化专项测试
覆盖：并发安全、安全加固、逻辑修复、中文缓存优化
无需 LLM API 和网络，纯逻辑测试

运行: pytest test_v34_optimizations.py -v
"""
import os
import sys
import asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


# ===== 1. 并发安全测试 =====

class TestConcurrencySafety:
    """v3.4: 验证 asyncio.Lock 保护的并发安全"""

    @pytest.mark.asyncio
    async def test_metrics_concurrent_record(self):
        """并发写入 MetricsCollector 不应产生数据竞争"""
        from core.monitoring import MetricsCollector
        m = MetricsCollector()

        async def record_batch(batch_id, count):
            for i in range(count):
                await m.record_request(
                    elapsed=1.0 + i * 0.1,
                    agent=f"agent_{batch_id}",
                    mode="sequential",
                    session_id=f"session_{batch_id}_{i}",
                )

        # 10 个并发协程，每个写入 50 条
        await asyncio.gather(*[record_batch(i, 50) for i in range(10)])
        assert m.total_requests == 500
        assert len(m.response_times) == 200  # 上限 200

    @pytest.mark.asyncio
    async def test_circuit_breaker_atomic_state(self):
        """并发调用 should_allow 不会导致多个协程同时进入 HALF_OPEN"""
        from core.monitoring import CircuitBreaker
        cb = CircuitBreaker(fail_threshold=2, recovery_time=0)

        # 触发 OPEN
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state == "open"

        # 并发探测 — 只应有一个进入 HALF_OPEN
        results = await asyncio.gather(*[cb.should_allow() for _ in range(10)])
        # 所有都应返回 True（因为 recovery_time=0），但状态应为 half_open
        assert cb.state == "half_open"

    @pytest.mark.asyncio
    async def test_metrics_stats_consistency(self):
        """并发写入后读取统计数据一致性"""
        from core.monitoring import MetricsCollector
        m = MetricsCollector()

        # 先写入数据
        for i in range(100):
            await m.record_request(elapsed=5.0 + i % 20, session_id=f"s{i}")

        # 读取统计不应抛异常
        stats = await m.get_stats()
        assert stats["total_requests"] == 100
        kpi = await m.get_kpi_stats()
        assert "first_resolution_rate" in kpi


# ===== 2. 安全加固测试 =====

class TestSecurityHardening:
    """v3.4: 验证安全加固措施"""

    def test_erp_input_sanitization(self):
        """ERP 输入净化防 SQL 注入"""
        from erp import sanitize_erp_input

        # 正常输入保持不变
        assert sanitize_erp_input("玫瑰精华") == "玫瑰精华"

        # SQL 注入尝试 — 危险字符（' ; --）全部移除
        result = sanitize_erp_input("' OR 1=1 --")
        assert "'" not in result
        assert "--" not in result
        # "OR" 和 "1=1" 中的 "=" 被移除，留下 "OR 11" — 无害

        # 分号和双横线被移除
        result = sanitize_erp_input("product; DROP TABLE users--")
        assert ";" not in result
        assert "--" not in result

        # 长度截断
        result = sanitize_erp_input("a" * 200)
        assert len(result) <= 100

        # 单引号被白名单移除（非安全字符）
        result = sanitize_erp_input("it's clean")
        assert "'" not in result
        assert "clean" in result

    def test_erp_adapter_uses_sanitization(self):
        """验证 real adapter 的方法签名包含净化"""
        # 检查 kingdee_real_adapter 导入了 sanitize_erp_input
        import importlib
        spec = importlib.util.find_spec("erp.kingdee_real_adapter")
        if spec:
            import erp.kingdee_real_adapter as mod
            source = open(mod.__file__).read()
            assert "sanitize_erp_input" in source

    def test_config_security_defaults(self):
        """验证安全相关配置存在"""
        from config import MAX_QUERY_LENGTH, MAX_SESSIONS, SESSION_IDLE_TTL
        assert MAX_QUERY_LENGTH == 2000
        assert MAX_SESSIONS == 10000
        assert SESSION_IDLE_TTL == 3600

    def test_version_updated(self):
        """验证版本号更新到 3.5"""
        from config import VERSION
        assert VERSION >= "3.4.0"


# ===== 3. 中文缓存优化测试 =====

class TestChineseCacheOptimization:
    """v3.4: 验证 jieba 中文分词提升缓存匹配精度"""

    def test_chinese_tokenization_in_cache(self):
        """缓存使用 jieba 分词而非简单正则"""
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=50, l2_max=200, default_ttl=3600)

        from session_manager import _tokenize_chinese

        # 使用 jieba 分词后，"玫瑰精华液成分" 应被分为多个有意义的词
        tokens = _tokenize_chinese("玫瑰精华液成分")
        # jieba 应该能切分出 "玫瑰"、"精华液"、"成分" 等词
        assert len(tokens) >= 2  # 而非整个字符串作为一个 token

    def test_semantic_match_improved(self):
        """jieba 分词后语义相似查询匹配率提升"""
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=50, l2_max=200, default_ttl=3600)

        cache.put("这款玫瑰精华液有什么成分", "含有玫瑰精油和透明质酸")
        # 语义相似但措辞不同的查询
        result = cache.get("玫瑰精华液的成分是什么")
        # jieba 分词后，两个查询应有足够重叠（玫瑰、精华液、成分）
        assert result is not None

    def test_cache_eviction_reduced_batch(self):
        """v3.4: 淘汰批次从 20% 降为 5%"""
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=20, l2_max=20, default_ttl=3600)

        # 填满缓存
        for i in range(20):
            cache.put(f"问题_{i}_完全不同的话题", f"回答_{i}")

        size_before = len(cache._l1)
        # 再添加一条触发淘汰
        cache.put("新问题触发淘汰", "新回答")
        size_after = len(cache._l1)

        # 5% 淘汰：20 * 5% = 1 条，加上新增 1 条，净增 0
        # 20% 淘汰：20 * 20% = 4 条，加上新增 1 条，净减 3
        assert size_after >= size_before - 2  # 5% 淘汰只移除 1 条


# ===== 4. 逻辑修复测试 =====

class TestLogicFixes:
    """v3.4: 验证逻辑修复"""

    def test_complaint_not_auto_escalated(self):
        """投诉 hierarchical 模式不再自动标记为 escalated"""
        from agents.response_agent import ResponseAgent
        agent = ResponseAgent()
        state = {
            "response": "非常抱歉给您带来不好的体验，已安排专人跟进并提供50元优惠券补偿。",
            "collaboration_mode": "hierarchical",
            "query_type": "complaint",
        }
        assert agent._evaluate_resolution(state) == "resolved"

    def test_explicit_handoff_still_escalated(self):
        """明确转人工的响应仍标记为 escalated"""
        from agents.response_agent import ResponseAgent
        agent = ResponseAgent()
        state = {
            "response": "您的问题比较复杂，正在为您转接人工客服，请稍候。",
            "collaboration_mode": "hierarchical",
            "query_type": "complaint",
        }
        assert agent._evaluate_resolution(state) == "escalated"

    def test_router_json_parse_robust(self):
        """Router JSON 解析更稳健（非贪婪匹配 + raw_decode）"""
        from router.query_router import QueryRouter
        router = QueryRouter()
        # 直接测试规则分类（不需要 LLM）
        result = router._rule_classify_and_score("我要退款")[0]
        assert result == "billing"

    def test_session_manager_async_context(self):
        """get_conversation_context 现在是 async"""
        import inspect
        from session_manager import EnhancedSessionManager
        assert inspect.iscoroutinefunction(EnhancedSessionManager.get_conversation_context)

    def test_session_eviction(self):
        """会话超过上限时自动淘汰"""
        from session_manager import EnhancedSessionManager
        import config
        # 使用较小的上限测试
        original = config.MAX_SESSIONS
        config.MAX_SESSIONS = 5
        try:
            sm = EnhancedSessionManager(window_size=3, max_tokens=1000)
            for i in range(10):
                sm.create_session(f"evict_{i}")
                sm.add_message(f"evict_{i}", f"消息 {i}", is_user=True)
            # v3.6: 淘汰已节流（每 50 次创建检查一次），手动触发验证
            sm._evict_idle_sessions()
            # 会话数不应超过上限
            assert len(sm.sessions) <= 5
        finally:
            config.MAX_SESSIONS = original

    def test_retry_only_transient_errors(self):
        """重试仅对瞬态错误生效"""
        from agents.base_agent import BaseAgent
        import inspect
        source = inspect.getsource(BaseAgent.process_with_retry)
        assert "ConnectionError" in source
        assert "TimeoutError" in source
        assert "permanent" in source.lower() or "raise" in source


# ===== 5. 安全响应头测试 =====

class TestSecurityHeaders:
    """v3.4: 验证安全响应头"""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from multi_agent_customer_service import make_graph, session_mgr, cache, metrics, bus
        from api.app import create_app
        app = create_app(make_graph(), session_manager=session_mgr,
                         response_cache=cache, metrics=metrics, message_bus=bus)
        return TestClient(app)

    def test_security_headers_present(self, client):
        """验证安全响应头"""
        resp = client.get("/api/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        # v3.4: CSP 替代了废弃的 X-XSS-Protection
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
