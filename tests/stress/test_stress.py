"""
压力/性能测试（v3.9）
验证系统在高并发和大数据量下的稳定性和性能。
所有测试无需外部依赖，可在 CI 中运行。
"""

import asyncio
import time

import pytest


@pytest.mark.stress
class TestCachePressure:
    """缓存压力测试（使用 L3 Jaccard fallback 模式，无需外部依赖）"""

    def test_cache_high_frequency_read_write(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        # P0-02: 显式公开 intent，走 SHARED 路径以真实测吞吐
        meta = {"intent_type": "chitchat"}

        start = time.time()
        for i in range(2000):
            cache.put(f"query_{i}", f"response_{i}", metadata=meta)
        for i in range(2000):
            cache.get(f"query_{i}")
        elapsed = time.time() - start

        assert elapsed < 5.0, f"Cache throughput too slow: {elapsed:.2f}s"

    def test_cache_eviction_under_pressure(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        meta = {"intent_type": "chitchat"}

        for i in range(500):
            cache.put(f"query_{i}", f"response_{i}", metadata=meta)

        stats = cache.get_stats()
        assert stats["l3_size"] <= 500

    def test_cache_concurrent_access(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        meta = {"intent_type": "chitchat"}

        for i in range(500):
            cache.put(f"q{i}", f"r{i}", metadata=meta)
        for i in range(500):
            cache.get(f"q{i}")

        stats = cache.get_stats()
        assert stats["l3_size"] > 0


@pytest.mark.stress
class TestMessageBusStress:
    """消息总线压力测试"""

    @pytest.mark.asyncio
    async def test_bus_high_concurrency(self):
        """高并发消息发布"""
        from core.message_bus import Message, MessageBus, MessageType

        bus = MessageBus()
        received = []

        async def handler(msg):
            received.append(msg)

        await bus.subscribe("stress_topic", handler)

        async def publish_batch(start_idx: int):
            for i in range(50):
                await bus.publish(
                    Message(
                        msg_type=MessageType.BROADCAST,
                        topic="stress_topic",
                        sender=f"sender_{start_idx}",
                        payload={"idx": start_idx * 50 + i},
                    )
                )

        await asyncio.gather(publish_batch(0), publish_batch(1), publish_batch(2), publish_batch(3))

        # 等待消息处理
        await asyncio.sleep(0.1)

        assert len(received) == 200, f"Expected 200 messages, got {len(received)}"


@pytest.mark.stress
class TestBlackboardStress:
    """共享黑板压力测试"""

    @pytest.mark.asyncio
    async def test_blackboard_concurrent_writes(self):
        """并发写入黑板"""
        from core.shared_blackboard import SharedBlackboard

        bb = SharedBlackboard()

        async def write_batch(prefix: str):
            for i in range(100):
                await bb.write(f"{prefix}_{i}", f"value_{i}", ttl=60)

        await asyncio.gather(write_batch("a"), write_batch("b"), write_batch("c"), write_batch("d"))

        # 读取部分数据验证
        val_a = await bb.read("a_0")
        val_d = await bb.read("d_99")
        assert val_a == "value_0"
        assert val_d == "value_99"


@pytest.mark.stress
class TestSessionStress:
    """会话管理压力测试"""

    def test_session_creation_throughput(self):
        """会话创建吞吐量"""
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        start = time.time()

        for i in range(1000):
            sm.create_session(f"session_{i}")

        elapsed = time.time() - start
        assert elapsed < 8.0, f"Session creation too slow: {elapsed:.2f}s"

    def test_session_message_throughput(self):
        """消息写入吞吐量"""
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        sm.create_session("stress_session")

        start = time.time()
        for i in range(5000):
            sm.add_message("stress_session", f"Message {i}", is_user=(i % 2 == 0))
        elapsed = time.time() - start

        assert elapsed < 15.0, f"Message throughput too slow: {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_session_context_retrieval(self):
        """大量消息下的上下文检索"""
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        await sm.create_session("stress_session")

        # 写入大量消息
        for i in range(100):
            await sm.add_message("stress_session", f"Message {i}", is_user=(i % 2 == 0))

        start = time.time()
        context = await sm.get_conversation_context("stress_session", max_messages=20)
        elapsed = time.time() - start

        # SessionManager includes summary + recent messages, total may exceed max_messages
        assert len(context) > 0
        assert elapsed < 1.0, f"Context retrieval too slow: {elapsed:.2f}s"


@pytest.mark.stress
class TestCircuitBreakerStress:
    """熔断器压力测试"""

    @pytest.mark.asyncio
    async def test_circuit_breaker_concurrent_checks(self):
        """并发检查熔断器状态"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=5, recovery_time=60)

        async def check_allowed():
            return await cb.should_allow()

        # 并发检查
        results = await asyncio.gather(*[check_allowed() for _ in range(100)])
        assert all(isinstance(r, bool) for r in results)

    @pytest.mark.asyncio
    async def test_circuit_breaker_state_transitions(self):
        """熔断器状态转换压力测试"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=3, recovery_time=0.1)

        # 触发熔断
        for _ in range(3):
            await cb.record_failure()

        assert cb.state == "open"

        # 等待恢复
        await asyncio.sleep(0.2)

        # 验证 HALF_OPEN
        allowed = await cb.should_allow()
        assert allowed is True
        assert cb.state == "half_open"


@pytest.mark.stress
class TestMetricsStress:
    """监控指标压力测试"""

    @pytest.mark.asyncio
    async def test_metrics_concurrent_recording(self):
        """并发记录监控指标"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()

        async def record_batch(start_idx: int):
            for i in range(100):
                await mc.record_request(
                    elapsed=0.5 + (i % 10) * 0.1,
                    error=(i % 20 == 0),
                )

        await asyncio.gather(record_batch(0), record_batch(1), record_batch(2), record_batch(3))

        stats = await mc.get_stats()
        assert stats["total_requests"] == 400


@pytest.mark.stress
class TestRoutingStress:
    """路由压力测试"""

    @pytest.mark.asyncio
    async def test_routing_throughput(self):
        """路由吞吐量"""
        from router.query_router import QueryRouter

        qr = QueryRouter()
        queries = [
            "你好",
            "精华液多少钱",
            "我要投诉",
            "退货退款",
            "过敏了怎么办",
            "产品成分是什么",
            "物流查询",
            "发票问题",
        ]

        start = time.time()
        for _ in range(100):
            for q in queries:
                await qr.route(q)
        elapsed = time.time() - start

        assert elapsed < 10.0, f"Routing throughput too slow: {elapsed:.2f}s"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
