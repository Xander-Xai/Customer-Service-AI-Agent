"""
压力测试 / 性能基准测试（v3.0）
验证系统在并发负载下的性能表现

运行: pytest test_stress.py -v -s
"""
import os
import sys
import asyncio
import time
import statistics

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


# ===== Fixtures =====

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.loop = loop


@pytest.fixture(scope="session")
def graph_app():
    from multi_agent_customer_service import make_graph
    return make_graph()


# ===== 1. 缓存压力测试 =====

class TestCacheStress:
    """缓存系统在高频读写下的一致性和性能"""

    def test_high_frequency_put_get(self):
        from cache.response_cache import ResponseCache
        c = ResponseCache(l1_max=100, l2_max=500, default_ttl=60)

        # 写入 200 条（超过 L1 容量）
        start = time.time()
        for i in range(200):
            c.put(f"stress_q_{i}", f"stress_a_{i}")
        put_time = time.time() - start

        # 读取最新 100 条（应大部分在 L1 中）
        hits = 0
        start = time.time()
        for i in range(100, 200):
            if c.get(f"stress_q_{i}") is not None:
                hits += 1
        get_time = time.time() - start

        stats = c.get_stats()
        print(f"\n    缓存压力: 200次写入 {put_time:.3f}s, 100次读取 {get_time:.3f}s, 命中 {hits}/100")
        print(f"    L1={stats['l1_size']}, L2={stats['l2_size']}")
        assert hits >= 50  # 至少 50% 命中（淘汰后剩余）

    def test_concurrent_cache_access(self):
        """并发缓存读写不应导致异常"""
        from cache.response_cache import ResponseCache
        c = ResponseCache(l1_max=50, l2_max=200, default_ttl=60)

        async def writer(n):
            for i in range(20):
                c.put(f"concurrent_{n}_{i}", f"val_{n}_{i}")

        async def reader(n):
            for i in range(20):
                c.get(f"concurrent_{n}_{i}")

        async def run():
            tasks = []
            for n in range(5):
                tasks.append(writer(n))
                tasks.append(reader(n))
            await asyncio.gather(*tasks)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()
        print(f"\n    并发缓存: 10个并发任务完成，无异常")


# ===== 2. 消息总线压力测试 =====

class TestBusStress:
    """消息总线在高频发布下的性能"""

    @pytest.mark.asyncio
    async def test_high_frequency_publish(self):
        from core.message_bus import MessageBus, Message, MessageType
        bus = MessageBus()
        received = []

        async def handler(msg):
            received.append(msg.payload)

        await bus.subscribe("stress.topic", handler)

        start = time.time()
        for i in range(100):
            await bus.publish(Message(
                msg_type=MessageType.BROADCAST,
                topic="stress.topic",
                payload=f"event_{i}"
            ))
        elapsed = time.time() - start

        print(f"\n    消息总线: 100次发布 {elapsed:.3f}s, 接收 {len(received)}/100")
        assert len(received) == 100
        assert elapsed < 1.0  # 100次发布应 < 1秒

    @pytest.mark.asyncio
    async def test_multi_topic_concurrent(self):
        from core.message_bus import MessageBus, Message, MessageType
        bus = MessageBus()
        counters = {f"topic_{i}": 0 for i in range(5)}

        for topic in counters:
            async def make_handler(t):
                async def handler(msg):
                    counters[t] += 1
                return handler
            await bus.subscribe(topic, await make_handler(topic))

        async def publish_all():
            tasks = []
            for i in range(5):
                async def make_publisher(idx):
                    for j in range(20):
                        await bus.publish(Message(
                            msg_type=MessageType.BROADCAST,
                            topic=f"topic_{idx}",
                            payload=f"msg_{j}"
                        ))
                tasks.append(make_publisher(i))
            await asyncio.gather(*tasks)

        await publish_all()
        total = sum(counters.values())
        print(f"\n    多主题并发: 5主题×20消息, 总接收 {total}")
        assert total == 100


# ===== 3. 黑板压力测试 =====

class TestBlackboardStress:
    """共享黑板在高频并发读写下的一致性"""

    @pytest.mark.asyncio
    async def test_concurrent_write_read(self):
        from core.shared_blackboard import SharedBlackboard
        bb = SharedBlackboard()

        async def writer(prefix, count):
            for i in range(count):
                await bb.write(f"{prefix}.{i}", f"value_{i}", ttl=60)

        async def reader(prefix, count):
            results = []
            for i in range(count):
                val = await bb.read(f"{prefix}.{i}")
                results.append(val)
            return results

        # 并发写入
        await asyncio.gather(
            writer("stress.a", 50),
            writer("stress.b", 50),
            writer("stress.c", 50),
        )

        # 验证数据完整性
        a_results = await reader("stress.a", 50)
        b_results = await reader("stress.b", 50)
        c_results = await reader("stress.c", 50)

        a_valid = sum(1 for r in a_results if r is not None)
        b_valid = sum(1 for r in b_results if r is not None)
        c_valid = sum(1 for r in c_results if r is not None)

        print(f"\n    黑板并发: A={a_valid}/50, B={b_valid}/50, C={c_valid}/50")
        assert a_valid == 50
        assert b_valid == 50
        assert c_valid == 50

    @pytest.mark.asyncio
    async def test_prefix_read_performance(self):
        from core.shared_blackboard import SharedBlackboard
        bb = SharedBlackboard()

        # 写入 500 条
        for i in range(500):
            await bb.write(f"perf.key_{i}", f"value_{i}", ttl=300)

        start = time.time()
        results = await bb.read_prefix("perf.")
        elapsed = time.time() - start

        print(f"\n    黑板前缀读取: 500条中匹配 {len(results)} 条, 耗时 {elapsed:.4f}s")
        assert len(results) == 500
        assert elapsed < 0.1  # 前缀读取应 < 100ms


# ===== 4. 会话管理压力测试 =====

class TestSessionStress:
    """会话管理器在大量会话下的性能"""

    def test_many_sessions(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager(window_size=5, max_tokens=2000)

        start = time.time()
        for i in range(100):
            sid = f"stress_session_{i}"
            sm.create_session(sid)
            for j in range(10):
                sm.add_message(sid, f"消息 {j} from session {i}", is_user=(j % 2 == 0))
        create_time = time.time() - start

        # 批量获取上下文（v3.4: get_conversation_context 改为 async）
        start = time.time()
        loop = asyncio.new_event_loop()
        for i in range(100):
            loop.run_until_complete(sm.get_conversation_context(f"stress_session_{i}"))
        loop.close()
        context_time = time.time() - start

        sessions = sm.list_sessions()
        print(f"\n    会话压力: 创建100个会话(各10消息) {create_time:.3f}s, 获取上下文 {context_time:.3f}s")
        print(f"    总会话数: {len(sessions)}")
        assert create_time < 5.0
        assert context_time < 2.0

    def test_drift_detection_performance(self):
        """漂移检测在长会话下的性能"""
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager(window_size=5, max_tokens=2000)
        sm.create_session("drift_perf")

        # 建立较长对话历史
        for i in range(30):
            sm.add_message("drift_perf", f"第{i+1}轮用户消息，关于玫瑰精华液的问题", is_user=True)
            sm.add_message("drift_perf", f"第{i+1}轮回复，298元", is_user=False)

        # 多次漂移检测
        start = time.time()
        for _ in range(20):
            sm.detect_drift("drift_perf", "我要投诉产品有问题")
        elapsed = time.time() - start

        print(f"\n    漂移检测性能: 20次检测(30轮历史) {elapsed:.3f}s")
        assert elapsed < 2.0


# ===== 5. Agent 并发处理测试 =====

class TestAgentStress:
    """Agent 在并发调用下的行为"""

    @pytest.mark.asyncio
    async def test_consequential_sequential(self):
        """顺序模式下多轮请求"""
        from multi_agent_customer_service import make_graph, initialize_agents
        app = make_graph()
        await initialize_agents()

        queries = [
            "你好",
            "有什么产品推荐",
            "精华液多少钱",
        ]

        results = []
        start = time.time()
        for q in queries:
            state = {
                "session_id": "stress_seq",
                "current_agent": "",
                "customer_query": q,
                "query_type": "",
                "response": "",
                "complexity": 0,
                "fast_path": True,
                "collaboration_mode": "",
                "cached": False,
                "agents_used": [],
                "resolution_status": "",
            }
            result = await app.ainvoke(state)
            results.append(result)
        elapsed = time.time() - start

        print(f"\n    顺序3轮: {elapsed:.2f}s, 平均 {elapsed/3:.2f}s/轮")
        for i, r in enumerate(results):
            assert r["response"], f"第{i+1}轮无响应"


# ===== 6. API 端点压力测试 =====

class TestAPIStress:
    """FastAPI 端点在并发请求下的表现"""

    def test_sequential_chat_requests(self, graph_app):
        """顺序多轮 chat 请求（v3.8: 复用会话需携带 session_token）"""
        from multi_agent_customer_service import session_mgr, cache, metrics, bus
        from api.app import create_app
        from fastapi.testclient import TestClient
        from config import API_KEY

        app = create_app(graph_app, session_manager=session_mgr, response_cache=cache, metrics=metrics, message_bus=bus)
        client = TestClient(app, raise_server_exceptions=True)
        headers = {"X-API-Key": API_KEY}

        queries = ["你好", "有什么产品", "价格多少"]
        times = []

        # v3.8: 首请求不携带 session_id，获取 session_id + session_token
        first_resp = client.post("/api/chat", json={"query": queries[0]}, headers=headers)
        assert first_resp.status_code == 200, f"首请求失败: {first_resp.text}"
        first_data = first_resp.json()
        sid = first_data.get("session_id") or first_data.get("session", {}).get("session_id", "api_stress")
        token = first_data.get("session_token", "")
        assert sid, "首响应无 session_id"

        # 后续请求携带 session_id + session_token
        for q in queries[1:]:
            start = time.time()
            resp = client.post("/api/chat", json={"query": q, "session_id": sid, "session_token": token}, headers=headers)
            elapsed = time.time() - start
            times.append(elapsed)
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        avg = statistics.mean(times)
        print(f"\n    API 顺序3轮: 平均 {avg:.2f}s, 范围 [{min(times):.2f}s, {max(times):.2f}s]")

    def test_health_endpoint_stress(self, graph_app):
        """健康检查端点应快速响应"""
        from multi_agent_customer_service import session_mgr, cache, metrics, bus
        from api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(graph_app, session_manager=session_mgr, response_cache=cache, metrics=metrics, message_bus=bus)
        client = TestClient(app)

        start = time.time()
        for _ in range(100):
            resp = client.get("/api/health")
            assert resp.status_code == 200
        elapsed = time.time() - start

        print(f"\n    健康检查: 100次 {elapsed:.3f}s, 平均 {elapsed/100*1000:.1f}ms/次")
        assert elapsed < 5.0  # 100次应 < 5秒


# ===== 7. 缓存命中率模拟 =====

class TestCacheHitRate:
    """模拟真实场景的缓存命中率"""

    def test_repeated_query_hit_rate(self):
        """重复查询应有高命中率"""
        from cache.response_cache import ResponseCache
        c = ResponseCache(l1_max=100, l2_max=500, default_ttl=3600)

        # 预热：20个常见问题
        common_queries = [
            "玫瑰精华液多少钱", "有什么产品推荐", "怎么退款",
            "产品过敏怎么办", "你们的地址在哪", "发货要几天",
            "可以开发票吗", "会员有什么优惠", "产品保质期多久",
            "怎么使用精华液", "有哪些护肤品", "适合油性皮肤的",
        ]
        for q in common_queries:
            c.put(q, f"回答: {q}")

        # 模拟 100 次查询（60% 重复 + 40% 新查询）
        import random
        random.seed(42)
        hits = 0
        for i in range(100):
            if random.random() < 0.6:
                # 重复查询
                q = random.choice(common_queries)
            else:
                # 新查询
                q = f"新问题_{i}"
            if c.get(q) is not None:
                hits += 1

        hit_rate = hits / 100 * 100
        print(f"\n    模拟命中率: {hit_rate:.0f}% (60%重复+40%新查询)")
        assert hit_rate >= 50  # 应有至少 50% 命中率


# ===== 运行入口 =====

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])
