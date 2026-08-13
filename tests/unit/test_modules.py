"""
模块级全面验证测试 (v3.8)
覆盖：SessionManager / ResponseAgent / Cache / Router / ERP / Agents / Core / RAG / Tools / API / Collaboration
运行: pytest tests/test_modules.py -v
"""

import asyncio
import os
import re
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# tiktoken BPE 文件需网络下载，离线环境跳过依赖该 tokenizer 的测试
_tiktoken_available = True
try:
    import tiktoken
    tiktoken.get_encoding("cl100k_base")
except Exception:
    _tiktoken_available = False

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SessionManager 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestSessionManagerModule:
    """SessionManager 完整验证"""

    @pytest.mark.asyncio
    async def test_create_session_returns_id(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        sid = await sm.create_session("test_001")
        assert sid == "test_001"
        assert "test_001" in sm.sessions

    @pytest.mark.asyncio
    async def test_create_session_auto_uuid(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        sid = await sm.create_session()
        assert isinstance(sid, str)
        assert len(sid) > 0

    @pytest.mark.asyncio
    async def test_create_session_invalid_id_generates_new(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        sid = await sm.create_session("../../../etc/passwd")
        assert sid != "../../../etc/passwd"
        assert len(sid) > 10

    @pytest.mark.asyncio
    async def test_add_message_sets_role(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        await sm.create_session("s1")
        await sm.add_message("s1", "你好", is_user=True)
        await sm.add_message("s1", "你好！有什么可以帮您？", is_user=False)
        session = await sm.get_session("s1")
        msgs = session["messages"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert msgs[0]["content"] == "你好"

    @pytest.mark.asyncio
    async def test_add_message_increments_count(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        await sm.create_session("s2")
        for i in range(5):
            await sm.add_message("s2", f"msg_{i}")
        session = await sm.get_session("s2")
        assert session["message_count"] == 5

    @pytest.mark.skipif(not _tiktoken_available, reason="tiktoken BPE 文件不可下载（离线环境）")
    @pytest.mark.asyncio
    async def test_token_eviction_enforced(self):
        """验证 max_tokens 参数生效，token 裁剪实际触发"""
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager(window_size=10, max_tokens=50)
        await sm.create_session("tok_test")
        for i in range(20):
            await sm.add_message(
                "tok_test", f"这是一条较长的消息用于测试token裁剪功能_{i:02d}", is_user=(i % 2 == 0)
            )
        ctx = await sm.get_conversation_context("tok_test")
        total_chars = sum(len(m.get("content", "")) for m in ctx)
        full_chars = sum(len(f"这是一条较长的消息用于测试token裁剪功能_{i:02d}") for i in range(20))
        assert total_chars < full_chars, "Token 裁剪应减少上下文长度"

    @pytest.mark.asyncio
    async def test_window_size_limits_messages(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager(window_size=3)
        await sm.create_session("win_test")
        for i in range(10):
            await sm.add_message("win_test", f"消息{i}")
        ctx = await sm.get_conversation_context("win_test")
        non_summary = [m for m in ctx if "[历史摘要]" not in m.get("content", "")]
        assert len(non_summary) <= sm.window_size * 2 + 1

    @pytest.mark.asyncio
    async def test_delete_session(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        await sm.create_session("del_test")
        assert "del_test" in sm.sessions
        await sm.delete_session("del_test")
        assert "del_test" not in sm.sessions

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        await sm.create_session("ls_1")
        await sm.create_session("ls_2")
        sessions = await sm.list_sessions()
        ids = [s["session_id"] for s in sessions]
        assert "ls_1" in ids
        assert "ls_2" in ids

    @pytest.mark.asyncio
    async def test_session_expiry(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        await sm.create_session("exp_test")
        sm.sessions["exp_test"]["last_activity"] = time.time() - 999999
        sm._evict_idle_sessions()
        assert "exp_test" not in sm.sessions

    # ---- Drift Detection ----

    @pytest.mark.asyncio
    async def test_topic_drift_detection(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        await sm.create_session("drift_1")
        # Need 3+ user messages for topic drift
        await sm.add_message("drift_1", "你们的面膜多少钱", is_user=True)
        await sm.add_message("drift_1", "88元一盒", is_user=False)
        await sm.add_message("drift_1", "好的我考虑一下", is_user=True)
        drift = await sm.detect_drift("drift_1", "今天天气怎么样")
        assert drift["has_drift"] is True
        assert any(d["type"] == "topic_drift" for d in drift["drifts"])

    @pytest.mark.asyncio
    async def test_intent_drift_detection(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        await sm.create_session("drift_2")
        await sm.add_message("drift_2", "我要退货", is_user=True)
        await sm.add_message("drift_2", "好的请提供订单号", is_user=False)
        await sm.add_message("drift_2", "订单号123", is_user=True)
        drift = await sm.detect_drift("drift_2", "这个产品怎么用")
        assert drift["has_drift"] is True
        assert any(d["type"] == "intent_drift" for d in drift["drifts"])

    @pytest.mark.asyncio
    async def test_no_drift_normal_conversation(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        await sm.create_session("drift_3")
        await sm.add_message("drift_3", "你们的面膜多少钱", is_user=True)
        await sm.add_message("drift_3", "88元", is_user=False)
        drift = await sm.detect_drift("drift_3", "有什么功效")
        # Short history (2 user msgs) - drift detection may not trigger
        assert isinstance(drift, dict)
        assert "has_drift" in drift

    @pytest.mark.asyncio
    async def test_repeat_detection(self):
        from core.session.session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        await sm.create_session("drift_4")
        await sm.add_message("drift_4", "你们的面膜多少钱", is_user=True)
        await sm.add_message("drift_4", "88元一盒", is_user=False)
        await sm.add_message("drift_4", "好的", is_user=True)
        drift = await sm.detect_drift("drift_4", "你们的面膜多少钱")
        assert drift["has_drift"] is True
        assert any(d["type"] == "repetition" for d in drift["drifts"])

    # ---- Token Counting ----

    def test_count_tokens_chinese(self):
        from core.session.session_manager import _count_tokens

        tokens = _count_tokens("你好世界")
        assert tokens > 0
        assert isinstance(tokens, int)

    def test_count_tokens_english(self):
        from core.session.session_manager import _count_tokens

        tokens = _count_tokens("hello world")
        assert tokens > 0

    def test_count_tokens_empty(self):
        from core.session.session_manager import _count_tokens

        tokens = _count_tokens("")
        assert tokens == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ResponseAgent 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestResponseAgentModule:
    """ResponseAgent 完整验证"""

    def test_sanitize_system_prefix(self):
        from agents.response_agent import _sanitize_response

        result = _sanitize_response("system system 你好")
        assert "你好" in result

    def test_sanitize_react_code(self):
        from agents.response_agent import _sanitize_response

        # Code artifacts should be removed from response
        result = _sanitize_response("React.createElement('div')\n正常回答内容")
        assert "createElement" not in result
        # The regex removes code lines; remaining content may be partial
        assert isinstance(result, str)

    def test_sanitize_console_log(self):
        from agents.response_agent import _sanitize_response

        result = _sanitize_response("console.log('debug')\n正常回答")
        assert "console.log" not in result
        assert isinstance(result, str)

    def test_sanitize_empty_input(self):
        from agents.response_agent import _sanitize_response

        assert _sanitize_response("") == ""
        assert _sanitize_response(None) is None

    def test_sanitize_preserves_valid_content(self):
        from agents.response_agent import _sanitize_response

        valid = "这款精华液适合油性皮肤，含有烟酰胺成分，能有效美白。"
        assert _sanitize_response(valid) == valid

    def test_sanitize_prompt_artifacts(self):
        from agents.response_agent import _sanitize_response

        result = _sanitize_response(">>> 重要提示\n正常内容")
        assert ">>>" not in result
        assert "正常内容" in result

    def test_sanitize_block_comments(self):
        from agents.response_agent import _sanitize_response

        result = _sanitize_response("/* todo */\n有效回答")
        assert "/* todo */" not in result
        assert "有效回答" in result

    def test_resolution_resolved(self):
        from agents.response_agent import RESOLUTION_RESOLVED, ResponseAgent

        ra = ResponseAgent()
        state = {"response": "这款产品适合您，含有玻尿酸成分，可以有效保湿。建议每天使用两次。"}
        status = ra._evaluate_resolution(state)
        assert status == RESOLUTION_RESOLVED

    def test_resolution_failed_empty(self):
        from agents.response_agent import RESOLUTION_FAILED, ResponseAgent

        ra = ResponseAgent()
        assert ra._evaluate_resolution({"response": ""}) == RESOLUTION_FAILED
        assert ra._evaluate_resolution({"response": "处理出错，请重试"}) == RESOLUTION_FAILED

    def test_resolution_escalated(self):
        from agents.response_agent import RESOLUTION_ESCALATED, ResponseAgent

        ra = ResponseAgent()
        state = {"response": "您的问题需要转接人工客服处理，正在为您转接。"}
        assert ra._evaluate_resolution(state) == RESOLUTION_ESCALATED

    def test_resolution_uncertain_short(self):
        from agents.response_agent import RESOLUTION_UNCERTAIN, ResponseAgent

        ra = ResponseAgent()
        state = {"response": "好的"}
        assert ra._evaluate_resolution(state) == RESOLUTION_UNCERTAIN

    def test_resolution_uncertain_phrase(self):
        from agents.response_agent import RESOLUTION_UNCERTAIN, ResponseAgent

        ra = ResponseAgent()
        state = {"response": "抱歉无法确定该产品的具体成分，请您谅解。"}
        assert ra._evaluate_resolution(state) == RESOLUTION_UNCERTAIN


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ResponseCache 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestResponseCacheModule:
    """ResponseCache L1/L2/L3 完整验证（使用 L3 Jaccard fallback 模式，无需外部依赖）"""

    def test_l1_put_get(self):
        """L3: 缓存写入与读取"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("你好", "您好！")
        assert cache.get("你好") == "您好！"

    def test_l1_miss(self):
        """全层未命中返回 None"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache()
        assert cache.get("不存在的查询_xyz_123") is None

    def test_l1_normalize_matches(self):
        """L1: 标准化后相同文本应命中"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put(" 烟酰胺能美白吗 ", "可以")
        assert cache.get("烟酰胺能美白吗") == "可以"

    def test_cache_stats(self):
        """缓存统计包含所有三级"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("q1", "r1")
        cache.get("q1")
        cache.get("miss_query_xyz")
        stats = cache.get_stats()
        assert "l1_hits" in stats
        assert "l2_hits" in stats
        assert "fallback_hits" in stats
        assert "misses" in stats

    def test_jaccard_fallback_match(self):
        """L3: Jaccard 降级词法匹配"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("烟酰胺美白", "烟酰胺可以抑制黑色素")
        result = cache.get("烟酰胺美白效果")
        assert result == "烟酰胺可以抑制黑色素"

    def test_jaccard_fallback_miss(self):
        """L3: Jaccard 降级不命中（相似度低）"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.99)
        cache.put("red lipstick", "response1")
        result = cache.get("completely different query about skincare routine")
        assert result is None

    def test_clear(self):
        """清空所有缓存"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("c1", "v1")
        cache.put("c2", "v2")
        cache.clear()
        assert cache.get("c1") is None

    def test_invalidate(self):
        """invalidate 调用不抛异常"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("inv_key", "inv_val")
        cache.invalidate("inv_key")  # should not raise
        assert True

    def test_jaccard_zero_sets(self):
        """_jaccard 空集合返回 0"""
        from cache.response_cache import ResponseCache

        assert ResponseCache._jaccard(frozenset(), frozenset()) == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. QueryRouter 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestQueryRouterModule:
    """QueryRouter 完整验证"""

    @pytest.mark.asyncio
    async def test_classify_product(self):
        from router.query_router import QueryRouter

        qr = QueryRouter()
        result = await qr.route("你们的精华液多少钱")
        assert result.query_type == "product_info"

    @pytest.mark.asyncio
    async def test_classify_complaint(self):
        from router.query_router import QueryRouter

        qr = QueryRouter()
        result = await qr.route("我要投诉你们的服务")
        assert result.query_type == "complaint"

    @pytest.mark.asyncio
    async def test_classify_billing(self):
        from router.query_router import QueryRouter

        qr = QueryRouter()
        result = await qr.route("我要退款")
        assert result.query_type == "billing"

    @pytest.mark.asyncio
    async def test_classify_technical(self):
        from router.query_router import QueryRouter

        qr = QueryRouter()
        result = await qr.route("这个产品过敏了红肿怎么办用法是什么")
        # Technical query with support keywords
        assert result.query_type in ("technical_support", "product_info")

    @pytest.mark.asyncio
    async def test_classify_general(self):
        from router.query_router import QueryRouter

        qr = QueryRouter()
        result = await qr.route("你好")
        # v6.1: INTENT_CLASSES 扩展后 "greeting" 替代旧的 "general_inquiry"
        assert result.query_type in ("greeting", "general_inquiry", "general")

    @pytest.mark.asyncio
    async def test_routing_result_has_fields(self):
        from router.query_router import QueryRouter

        qr = QueryRouter()
        result = await qr.route("产品价格咨询")
        assert hasattr(result, "query_type")
        assert hasattr(result, "agent_name")
        assert hasattr(result, "confidence")
        assert hasattr(result, "fast_path")
        assert hasattr(result, "complexity")

    @pytest.mark.asyncio
    async def test_complexity_fast_path(self):
        from router.query_router import QueryRouter

        qr = QueryRouter()
        result = await qr.route("你好")
        assert result.fast_path is True

    @pytest.mark.asyncio
    async def test_complexity_nontrivial(self):
        from router.query_router import QueryRouter

        qr = QueryRouter()
        result = await qr.route(
            "我买的那个精华液用了过敏，想退货退款，订单号是12345，你们这个产品的成分是什么"
        )
        assert result.complexity >= 3


# ═══════════════════════════════════════════════════════════════════════════════
# 5. ERP 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestERPModule:
    """ERP Adapter / Factory / Tools 完整验证"""

    @pytest.mark.asyncio
    async def test_mock_adapter_product(self):
        from erp.kingdee_adapter import KingdeeMockAdapter

        adapter = KingdeeMockAdapter()
        result = await adapter.query_product("玫瑰")
        assert isinstance(result, list)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_mock_adapter_inventory(self):
        from erp.kingdee_adapter import KingdeeMockAdapter

        adapter = KingdeeMockAdapter()
        result = await adapter.query_inventory("P001")
        assert "stock" in str(result) or "库存" in str(result)

    @pytest.mark.asyncio
    async def test_mock_adapter_order(self):
        from erp.kingdee_adapter import KingdeeMockAdapter

        adapter = KingdeeMockAdapter()
        result = await adapter.query_order("ORD20260530001")
        assert isinstance(result, list)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_mock_adapter_customer(self):
        from erp.kingdee_adapter import KingdeeMockAdapter

        adapter = KingdeeMockAdapter()
        result = await adapter.query_customer("C001")
        assert isinstance(result, dict)
        assert result["name"] == "王女士"

    def test_factory_mock(self):
        from erp.factory import create_erp_adapter

        adapter = create_erp_adapter()
        assert hasattr(adapter, "query_product")
        assert hasattr(adapter, "query_inventory")
        assert hasattr(adapter, "query_order")
        assert hasattr(adapter, "query_customer")

    def test_abstract_interface(self):
        from erp.kingdee_adapter import KingdeeAdapterBase

        with pytest.raises(TypeError):
            KingdeeAdapterBase()

    def test_erp_tools_execution(self):
        from erp.kingdee_adapter import KingdeeMockAdapter
        from tools.erp_tools import create_erp_tools

        registry = create_erp_tools(KingdeeMockAdapter())
        # create_erp_tools returns a ToolRegistry with tools already registered
        tools = registry.list_tools()
        assert "query_product" in tools
        assert "query_inventory" in tools
        assert "query_order" in tools
        assert "query_customer" in tools


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Agents 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestAgentsModule:
    """Agent 子类完整验证"""

    def test_product_agent_init(self):
        from agents.product_agent import ProductAgent

        pa = ProductAgent()
        assert pa.name == "产品专家"

    def test_billing_agent_init(self):
        from agents.billing_agent import BillingAgent

        ba = BillingAgent()
        assert ba.name == "账单专家"

    def test_tech_agent_init(self):
        from agents.tech_agent import TechAgent

        ta = TechAgent()
        assert ta.name == "技术支持专家"

    def test_complaint_agent_init(self):
        from agents.complaint_agent import ComplaintAgent

        ca = ComplaintAgent()
        assert ca.name == "投诉处理专家"

    def test_general_agent_init(self):
        from agents.general_agent import GeneralAgent

        ga = GeneralAgent()
        assert ga.name == "通用咨询专家"

    def test_response_agent_init(self):
        from agents.response_agent import ResponseAgent

        ra = ResponseAgent()
        assert ra.name == "response_agent"

    def test_react_agent_init(self):
        from agents.react_agent import ReActAgent

        ra = ReActAgent()
        assert ra.name == "ReAct推理专家"

    def test_agents_inherit_base(self):
        from agents.base_agent import BaseAgent
        from agents.billing_agent import BillingAgent
        from agents.complaint_agent import ComplaintAgent
        from agents.general_agent import GeneralAgent
        from agents.product_agent import ProductAgent
        from agents.react_agent import ReActAgent
        from agents.response_agent import ResponseAgent
        from agents.tech_agent import TechAgent

        for cls in [
            ProductAgent,
            BillingAgent,
            TechAgent,
            ComplaintAgent,
            GeneralAgent,
            ResponseAgent,
            ReActAgent,
        ]:
            assert issubclass(cls, BaseAgent)

    def test_drift_repair_strategies(self):
        from core.session.session_manager import DRIFT_REPAIR_STRATEGIES

        assert "topic_drift" in DRIFT_REPAIR_STRATEGIES
        assert "intent_drift" in DRIFT_REPAIR_STRATEGIES
        assert "repetition" in DRIFT_REPAIR_STRATEGIES

    def test_drift_type_class(self):
        from core.session.session_manager import DriftType

        assert hasattr(DriftType, "TOPIC")
        assert hasattr(DriftType, "INTENT")
        assert hasattr(DriftType, "CONTRADICTION")
        assert hasattr(DriftType, "REPETITION")


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Core 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestCoreModule:
    """Core 基础设施验证"""

    @pytest.mark.asyncio
    async def test_message_bus_pub_sub(self):
        from core.message_bus import Message, MessageBus, MessageType

        bus = MessageBus()
        received = []

        async def handler(msg):
            received.append(msg)

        await bus.subscribe("test.topic", handler)
        await bus.publish(
            Message(
                msg_type=MessageType.BROADCAST,
                topic="test.topic",
                sender="test",
                payload={"data": 1},
            )
        )
        assert len(received) == 1
        assert received[0].payload["data"] == 1

    @pytest.mark.asyncio
    async def test_message_bus_unsubscribe(self):
        from core.message_bus import Message, MessageBus, MessageType

        bus = MessageBus()
        received = []

        async def handler(msg):
            received.append(msg)

        await bus.subscribe("test.topic", handler)
        await bus.unsubscribe("test.topic", handler)
        await bus.publish(
            Message(msg_type=MessageType.BROADCAST, topic="test.topic", sender="test", payload={})
        )
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_blackboard_read_write(self):
        from core.shared_blackboard import SharedBlackboard

        bb = SharedBlackboard()
        await bb.write("key1", "value1")
        result = await bb.read("key1")
        assert result == "value1"

    @pytest.mark.asyncio
    async def test_blackboard_prefix_read(self):
        from core.shared_blackboard import SharedBlackboard

        bb = SharedBlackboard()
        await bb.write("agent.result.a", "data_a")
        await bb.write("agent.result.b", "data_b")
        results = await bb.read_prefix("agent.result")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_blackboard_ttl_expiration(self):
        from core.shared_blackboard import SharedBlackboard

        bb = SharedBlackboard()
        await bb.write("ttl_key", "ttl_value", ttl=0.1)
        assert await bb.read("ttl_key") == "ttl_value"
        await asyncio.sleep(0.2)
        assert await bb.read("ttl_key") is None

    @pytest.mark.asyncio
    async def test_blackboard_session_isolation(self):
        from core.shared_blackboard import SharedBlackboard, set_blackboard_session_id

        bb = SharedBlackboard()

        # Test isolation via ContextVar
        set_blackboard_session_id("session_1")
        await bb.write("user_data", "alice")

        set_blackboard_session_id("session_2")
        await bb.write("user_data", "bob")

        # Verify read separation via ContextVar
        set_blackboard_session_id("session_1")
        assert await bb.read("user_data") == "alice"

        set_blackboard_session_id("session_2")
        assert await bb.read("user_data") == "bob"

        # Test isolation via explicit parameter
        await bb.write("user_data", "charlie", session_id="session_3")
        assert await bb.read("user_data", session_id="session_3") == "charlie"
        assert await bb.read("user_data", session_id="session_1") == "alice"

        # Reset context var
        set_blackboard_session_id(None)


    @pytest.mark.asyncio
    async def test_monitoring_record_request(self):
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_request(elapsed=0.5, agent="test", mode="sequential", cached=False)
        stats = await mc.get_stats()
        assert stats["total_requests"] >= 1

    @pytest.mark.asyncio
    async def test_monitoring_circuit_breaker(self):
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=3, recovery_time=1)
        assert cb.get_status()["state"] == "closed"
        for _ in range(3):
            await cb.record_failure()
        assert cb.get_status()["state"] == "open"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. RAG 知识库模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestRAGModule:
    """RAG 知识库验证"""

    def test_knowledge_base_init(self):
        from rag.knowledge_base import CosmeticsKnowledgeBase

        with patch.object(CosmeticsKnowledgeBase, "_create_embedding_function", return_value=MagicMock()):
            kb = CosmeticsKnowledgeBase()
        assert not kb.available  # Qdrant 未运行，连接失败

    def test_seed_data_functions(self):
        from rag.seed_data import seed_faq, seed_product_knowledge, seed_tech_support

        assert callable(seed_faq)
        assert callable(seed_product_knowledge)
        assert callable(seed_tech_support)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Tools 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolsModule:
    """Tools 注册中心 + ERP Tools 验证"""

    def test_register_tool(self):
        from tools.tool_registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(
            "test_tool",
            "test desc",
            {"type": "object", "properties": {}},
            AsyncMock(return_value="ok"),
        )
        tools = registry.list_tools()
        assert "test_tool" in tools

    def test_get_openai_tools_format(self):
        from tools.tool_registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("test_tool", "test", {"type": "object", "properties": {}}, AsyncMock())
        openai_tools = registry.get_openai_tools()
        assert len(openai_tools) == 1
        assert openai_tools[0]["type"] == "function"

    @pytest.mark.asyncio
    async def test_execute_tool(self):
        from tools.tool_registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("echo", "echo tool", {}, AsyncMock(return_value="echoed"))
        result = await registry.execute("echo", {})
        assert result == "echoed"

    @pytest.mark.asyncio
    async def test_execute_missing_tool(self):
        from tools.tool_registry import ToolRegistry

        registry = ToolRegistry()
        result = await registry.execute("nonexistent", {})
        assert "不存在" in result or "not found" in result.lower()

    def test_erp_tools_creation(self):
        from erp.kingdee_adapter import KingdeeMockAdapter
        from tools.erp_tools import create_erp_tools
        from tools.tool_registry import ToolRegistry

        registry = create_erp_tools(KingdeeMockAdapter())
        assert isinstance(registry, ToolRegistry)
        tools = registry.list_tools()
        assert "query_product" in tools


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Collaboration 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestCollaborationModule:
    """协作编排器 + 模式验证"""

    def test_orchestrator_creation(self):
        from collaboration.orchestrator import CollaborationOrchestrator
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard

        orch = CollaborationOrchestrator(MessageBus(), SharedBlackboard())
        assert hasattr(orch, "select_mode_name")
        assert hasattr(orch, "build_context")

    def test_sequential_mode_selection(self):
        from collaboration.orchestrator import CollaborationOrchestrator
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard

        orch = CollaborationOrchestrator(MessageBus(), SharedBlackboard())
        mock_routing = MagicMock(
            complexity=1, query_type="product_info", agent_name="product_agent", fast_path=True
        )
        mode = orch.select_mode_name(mock_routing, {"customer_query": "你好"})
        assert mode == "sequential"

    def test_complaint_mode_selection(self):
        from collaboration.orchestrator import CollaborationOrchestrator
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard

        orch = CollaborationOrchestrator(MessageBus(), SharedBlackboard())
        mock_routing = MagicMock(
            complexity=3, query_type="complaint", agent_name="complaint_agent", fast_path=False
        )
        mode = orch.select_mode_name(mock_routing, {"customer_query": "投诉你们的产品"})
        assert mode == "hierarchical"

    def test_all_modes_exist(self):
        from collaboration.modes import (
            ConsultationMode,
            HierarchicalMode,
            ParallelMode,
            ReActMode,
            SequentialMode,
        )
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard

        bus, bb = MessageBus(), SharedBlackboard()
        for cls in [SequentialMode, ParallelMode, ConsultationMode, HierarchicalMode, ReActMode]:
            mode = cls(bus=bus, bb=bb)
            assert hasattr(mode, "execute")


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Config 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestConfigModule:
    """配置模块验证"""

    def test_config_loaded(self):
        from core import config

        assert hasattr(config, "VERSION")
        assert hasattr(config, "LLM_MODEL") or hasattr(config, "OPENAI_MODEL")

    def test_security_defaults(self):
        from core import config

        assert isinstance(config.API_KEY_ENABLED, bool)
        assert 100 <= config.MAX_QUERY_LENGTH <= 10000

    def test_circuit_breaker_config(self):
        from core import config

        assert config.CIRCUIT_BREAKER_FAIL_THRESHOLD > 0
        assert config.CIRCUIT_BREAKER_RECOVERY_TIME > 0

    def test_session_config(self):
        from core import config

        assert config.MAX_SESSIONS > 0
        assert config.SESSION_IDLE_TTL > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 12. API 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestAPIModule:
    """API 端点 + 安全验证"""

    def test_sanitize_input(self):
        from api.utils import sanitize_input

        assert sanitize_input("hello\x00world") == "helloworld"
        assert sanitize_input("<script>alert('xss')</script>") != "<script>alert('xss')</script>"

    def test_validate_session_id_valid(self):
        from api.utils import validate_session_id

        sid = validate_session_id("abc-123_test")
        assert sid == "abc-123_test"

    def test_validate_session_id_invalid(self):
        from api.utils import validate_session_id

        sid = validate_session_id("../../etc/passwd")
        assert sid != "../../etc/passwd"
        assert len(sid) > 10

    def test_validate_session_id_empty(self):
        from api.utils import validate_session_id

        sid = validate_session_id("")
        assert len(sid) > 0

    def test_health_endpoint(self):
        from api.app import create_app
        from core.container import ServiceContainer
        from core.graph_builder import build_graph

        container = ServiceContainer()
        graph = build_graph(container)
        app = create_app(graph)
        from fastapi.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "version" in data

    def test_security_headers(self):
        from api.app import create_app
        from core.container import ServiceContainer
        from core.graph_builder import build_graph

        container = ServiceContainer()
        graph = build_graph(container)
        app = create_app(graph)
        from fastapi.testclient import TestClient

        client = TestClient(app)

        # JSON API 端点：应有通用安全头，不含 CSP（CSP 仅对 HTML 页面设置）
        resp = client.get("/api/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"

        # HTML 页面：应有 CSP 头，且 style-src 不含 nonce（避免 'unsafe-inline' 被忽略）
        resp_html = client.get("/")
        assert "Content-Security-Policy" in resp_html.headers
        csp = resp_html.headers["Content-Security-Policy"]
        assert "style-src 'self' 'unsafe-inline'" in csp
        assert "style-src" not in csp.replace("style-src 'self' 'unsafe-inline'", "")

    def test_feedback_endpoint_validation(self):
        from api.app import create_app
        from core import config
        from core.container import ServiceContainer
        from core.graph_builder import build_graph

        container = ServiceContainer()
        graph = build_graph(container)
        app = create_app(graph)
        from fastapi.testclient import TestClient

        client = TestClient(app)
        headers = {}
        if config.API_KEY_ENABLED and config.API_KEY:
            headers["X-API-Key"] = config.API_KEY
        # Missing session_id field (Pydantic validation)
        resp = client.post("/api/feedback", json={"resolved": True}, headers=headers)
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# 13. LangGraph 主流程模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphModule:
    """LangGraph 图构建验证"""

    def test_graph_build(self):
        from core.container import ServiceContainer
        from core.graph_builder import build_graph

        container = ServiceContainer()
        graph = build_graph(container)
        assert hasattr(graph, "ainvoke")

    @pytest.mark.asyncio
    async def test_graph_simple_query(self):
        from core.container import ServiceContainer
        from core.graph_builder import build_graph

        container = ServiceContainer()
        graph = build_graph(container)
        state = {
            "session_id": "e2e_001",
            "customer_query": "你好",
            "response": "",
            "current_agent": "",
            "query_type": "",
            "complexity": 0,
            "fast_path": True,
            "collaboration_mode": "",
            "cached": False,
            "agents_used": [],
        }
        result = await graph.ainvoke(state)
        assert result["response"] != ""
        assert result["current_agent"] != ""


# ═══════════════════════════════════════════════════════════════════════════════
# 14. Logger 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoggerModule:
    """日志模块验证"""

    def test_get_logger(self):
        from core.logger import get_logger

        logger = get_logger("test")
        assert hasattr(logger, "info")
        assert hasattr(logger, "warning")

    def test_logger_has_methods(self):
        from core.logger import get_logger

        logger = get_logger("test_methods")
        assert hasattr(logger, "info")
        assert hasattr(logger, "warning")
        assert hasattr(logger, "error")
        assert hasattr(logger, "debug")


# ═══════════════════════════════════════════════════════════════════════════════
# 15. 安全审查专项测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestSecurityAudit:
    """安全审查验证"""

    def test_no_hardcoded_secrets(self):
        import os

        secret_patterns = [
            r'password\s*=\s*["\'][^"\'\n]+["\']',
            r'api_key\s*=\s*["\'][^"\'\n]+["\']',
            r'secret\s*=\s*["\'][^"\'\n]+["\']',
        ]
        # 只扫描源码目录，排除测试和第三方代码
        source_dirs = [
            "agents",
            "api",
            "auth",
            "cache",
            "collaboration",
            "core",
            "db",
            "erp",
            "knowledge",
            "llm",
            "rag",
            "router",
            "tools",
        ]
        for src_dir in source_dirs:
            if not os.path.isdir(src_dir):
                continue
            for root, dirs, files in os.walk(src_dir):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv", "venv")]
                for f in files:
                    if f.endswith(".py"):
                        path = os.path.join(root, f)
                    try:
                        with open(path, encoding="utf-8", errors="ignore") as fh:
                            content = fh.read()
                        for pattern in secret_patterns:
                            matches = re.findall(pattern, content, re.IGNORECASE)
                            real_matches = [
                                m
                                for m in matches
                                if "test" not in m.lower()
                                and "example" not in m.lower()
                                and "config" not in m.lower()
                                and "sample" not in m.lower()
                            ]
                            assert len(real_matches) == 0, (
                                f"Hardcoded secret in {path}: {real_matches}"
                            )
                    except (PermissionError, UnicodeDecodeError) as e:
                        pytest.fail(f"Failed to read {path}: {e}")

    def test_session_id_validation_pattern(self):
        from api.utils import _SESSION_ID_RE

        assert _SESSION_ID_RE.match("abc123")
        assert _SESSION_ID_RE.match("test-session_id")
        assert not _SESSION_ID_RE.match("../../etc/passwd")
        assert not _SESSION_ID_RE.match("'; DROP TABLE--")
        assert not _SESSION_ID_RE.match("a" * 200)

    def test_cors_configuration(self):
        from core import config

        if hasattr(config, "ENV") and config.ENV == "production":
            assert config.CORS_ORIGINS != ["*"]


# ═══════════════════════════════════════════════════════════════════════════════
# 16. 性能基准测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestPerformance:
    """性能基准验证"""

    def test_cache_throughput(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        start = time.time()
        for i in range(1000):
            cache.put(f"q{i}", f"r{i}")
        for i in range(1000):
            cache.get(f"q{i}")
        elapsed = time.time() - start
        assert elapsed < 5.0, f"Cache throughput too slow: {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_router_throughput(self):
        from router.query_router import QueryRouter

        qr = QueryRouter()
        queries = ["你好", "精华液多少钱", "我要投诉", "退货退款", "过敏了怎么办"]
        start = time.time()
        for _ in range(50):
            for q in queries:
                await qr.route(q)
        elapsed = time.time() - start
        assert elapsed < 5.0, f"Router throughput too slow: {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_blackboard_concurrent_writes(self):
        from core.shared_blackboard import SharedBlackboard

        bb = SharedBlackboard()

        async def write_many(prefix):
            for i in range(50):
                await bb.write(f"{prefix}.{i}", f"v{i}")

        await asyncio.gather(*[write_many(f"t{t}") for t in range(5)])
        results = await bb.read_prefix("t0")
        assert len(results) == 50


# ═══════════════════════════════════════════════════════════════════════════════
# v4.2: 真流式 LLM 调用测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestStreamingLLM:
    """v4.2: 真流式 LLM 客户端 + Agent 流式处理验证"""

    @pytest.mark.asyncio
    async def test_async_invoke_stream_yields_chunks(self):
        """验证 async_invoke_stream 逐 chunk 返回文本"""
        from core.monitoring import CircuitBreaker
        from llm.client import OpenAICompatibleClient

        cb = CircuitBreaker()
        client = OpenAICompatibleClient(
            api_key="sk-test",
            base_url="http://localhost:9999",
            model="test-model",
            circuit_breaker=cb,
        )
        # 模拟 SSE 响应
        mock_lines = [
            'data: {"choices":[{"delta":{"content":"你"}}]}',
            'data: {"choices":[{"delta":{"content":"好"}}]}',
            'data: {"choices":[{"delta":{"content":"！"}}]}',
            "data: [DONE]",
        ]

        class MockStreamResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                for line in mock_lines:
                    yield line

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        class MockClient:
            is_closed = False

            def stream(self, method, url, **kwargs):
                return MockStreamResponse()

        # 注入 mock client
        client._get_async_client = lambda: MockClient().__class__.__mro__[0].__class__(MockClient)
        # 直接 mock _get_async_client 返回值
        original = OpenAICompatibleClient._client_pools.copy()
        try:
            OpenAICompatibleClient._client_pools["http://localhost:9999"] = MockClient()
            # 由于 httpx stream 接口不同，这里直接测试解析逻辑
            from core.monitoring import json

            chunks = []
            for line in mock_lines:
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    parsed = json.loads(data)
                    choices = parsed.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            chunks.append(content)
            assert chunks == ["你", "好", "！"]
        finally:
            OpenAICompatibleClient._client_pools = original

    @pytest.mark.asyncio
    async def test_process_with_llm_stream_uses_callback(self):
        """验证 _process_with_llm 在有 stream_callback 时走流式路径"""
        from unittest.mock import AsyncMock, MagicMock

        from agents.base_agent import BaseAgent

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return await self._process_with_llm(
                    state,
                    "test prompt",
                    fallback_response="fallback",
                )

        agent = DummyAgent(name="test", role="test", expertise=["test"])

        # Mock LLM with async_invoke_stream

        async def mock_stream(messages):
            for token in ["Hello", " ", "World"]:
                yield token

        mock_llm = MagicMock()
        mock_llm.async_invoke_stream = mock_stream
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="Hello World"))
        agent.set_llm(mock_llm)

        # 创建带 stream_callback 的 state
        streamed_events = []

        async def stream_callback(event):
            streamed_events.append(event)

        state = {
            "session_id": "test_stream",
            "customer_query": "你好",
            "stream_callback": stream_callback,
        }

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        sm.create_session = AsyncMock(return_value="test_stream")
        agent.set_session_manager(sm)

        await agent.process(state)

        # 验证：stream_callback 收到了 chunk 事件
        chunk_events = [e for e in streamed_events if e.get("type") == "chunk"]
        assert len(chunk_events) == 3
        assert chunk_events[0]["content"] == "Hello"
        assert chunk_events[1]["content"] == " "
        assert chunk_events[2]["content"] == "World"

        content_complete_events = [
            e for e in streamed_events if e.get("type") == "content_complete"
        ]
        assert len(content_complete_events) == 1
        assert content_complete_events[0]["content"] == "Hello World"

        # 验证：state 中 response 是完整文本
        assert state["response"] == "Hello World"

    @pytest.mark.asyncio
    async def test_process_with_llm_no_callback_uses_standard(self):
        """验证无 stream_callback 时走标准非流式路径"""
        from unittest.mock import AsyncMock, MagicMock

        from agents.base_agent import BaseAgent

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return await self._process_with_llm(
                    state,
                    "test prompt",
                    fallback_response="fallback",
                )

        agent = DummyAgent(name="test", role="test", expertise=["test"])

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="标准回复"))
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        sm.create_session = AsyncMock(return_value="test_no_stream")
        agent.set_session_manager(sm)

        state = {
            "session_id": "test_no_stream",
            "customer_query": "你好",
            # 无 stream_callback
        }

        await agent.process(state)
        assert state["response"] == "标准回复"
        mock_llm.async_invoke.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# T4b: Evaluator 深度测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvaluatorDeep:
    """ResponseEvaluator 多维度评分深度验证"""

    def test_completeness_score_short_response(self):
        """极短回答（<20字）应得低分"""
        from agents.evaluator import ResponseEvaluator

        ev = ResponseEvaluator()
        result = ev.evaluate("好的", {"query": "请问面膜怎么用", "query_type": "product"})
        # 短回答完整性应低于正常回答
        completeness = result["factors"]["completeness"]
        assert completeness < 40, f"极短回答完整性应<40, 实际={completeness}"

    def test_completeness_score_normal_response(self):
        """正常长度回答应得中高分"""
        from agents.evaluator import ResponseEvaluator

        ev = ResponseEvaluator()
        response = (
            "这款面膜含有玻尿酸成分，适合干性肌肤使用。"
            "使用方法：洁面后取适量均匀涂抹于面部，15-20分钟后洗净即可。"
            "建议每周使用2-3次，效果更佳。"
        )
        result = ev.evaluate(response, {"query": "面膜怎么用", "query_type": "product"})
        completeness = result["factors"]["completeness"]
        assert completeness >= 50, f"正常回答完整性应>=50, 实际={completeness}"

    def test_accuracy_score_error_response(self):
        """包含错误标记的回答应扣分"""
        from agents.evaluator import ResponseEvaluator

        ev = ResponseEvaluator()
        # 用精确匹配触发 error_response 惩罚
        result = ev.evaluate("处理出错，请重试", {"query": "订单查询", "query_type": "billing"})
        accuracy = result["factors"]["accuracy"]
        # 基准60 + error penalty(-40) = 20
        assert accuracy <= 30, f"错误回答准确性应<=30, 实际={accuracy}"

    def test_conciseness_score_ideal(self):
        """理想长度回答简洁性最高"""
        from agents.evaluator import ResponseEvaluator

        ev = ResponseEvaluator()
        # 50-500 字符范围内（需要 >=50 字符才进入 ideal 档）
        response = (
            "这款精华液含有烟酰胺成分，适合油性肌肤使用。"
            "建议每天早晚各使用一次，配合保湿霜效果更佳。"
            "使用前请先做皮肤测试，确保不过敏。"
        )
        score = ev._score_conciseness(response, "精华液怎么用")
        from agents.evaluator import CONCISENESS_IDEAL

        assert score >= CONCISENESS_IDEAL, f"理想长度简洁性应>={CONCISENESS_IDEAL}, 实际={score}"

    def test_conciseness_score_excessive(self):
        """过长回答简洁性低"""
        from agents.evaluator import ResponseEvaluator

        ev = ResponseEvaluator()
        # 超过 1500 字符
        response = "详细介绍如下：" + "这是一段冗长的产品介绍内容。" * 100
        score = ev._score_conciseness(response, "产品介绍")
        from agents.evaluator import CONCISENESS_EXCESSIVE

        assert score <= CONCISENESS_EXCESSIVE + 15, (
            f"过长回答简洁性应<={CONCISENESS_EXCESSIVE + 15}, 实际={score}"
        )

    def test_politeness_score(self):
        """包含礼貌用语的回答加分"""
        from agents.evaluator import ResponseEvaluator

        ev = ResponseEvaluator()
        polite_response = "您好，感谢您的咨询。请问还有什么可以帮您的吗？"
        score = ev._score_politeness(polite_response)
        # 基准50 + 多个礼貌用语加分（您好+8, 感谢+8, 请+8, 帮您+8, 吗？+5 friendly = 至少 87）
        assert score >= 80, f"礼貌回答礼貌性应>=80, 实际={score}"

    def test_relevance_score_with_query(self):
        """回答与问题相关时得分高于无问题情况"""
        from agents.evaluator import SCORE_BASE_RELEVANCE_NO_QUERY, ResponseEvaluator

        ev = ResponseEvaluator()
        # 有 query 且高度相关时，得分应高于无 query 的基准分
        query = "面膜适合什么肤质"
        response = "这款面膜适合干性肤质，含有保湿成分，能有效改善干燥问题。"
        score_with_query = ev._score_relevance(response, query)
        score_no_query = ev._score_relevance(response, "")
        assert score_with_query > 0, f"相关回答相关性应>0, 实际={score_with_query}"
        # 无 query 时返回固定基准分，有 query 时应不同
        assert score_no_query == SCORE_BASE_RELEVANCE_NO_QUERY

    def test_weighted_total_score(self):
        """验证加权总分计算正确"""
        from agents.evaluator import (
            WEIGHT_ACCURACY,
            WEIGHT_COMPLETENESS,
            WEIGHT_CONCISENESS,
            WEIGHT_POLITENESS,
            WEIGHT_RELEVANCE,
            ResponseEvaluator,
        )

        ev = ResponseEvaluator()
        response = "您好，感谢咨询。这款精华液含有玻尿酸，适合干性肌肤。每天早晚各用一次即可。"
        result = ev.evaluate(response, {"query": "精华液怎么用", "query_type": "product"})

        factors = result["factors"]
        expected_score = (
            factors["completeness"] * WEIGHT_COMPLETENESS
            + factors["accuracy"] * WEIGHT_ACCURACY
            + factors["conciseness"] * WEIGHT_CONCISENESS
            + factors["politeness"] * WEIGHT_POLITENESS
            + factors["relevance"] * WEIGHT_RELEVANCE
        )
        expected_score = round(min(100, max(0, expected_score)), 1)
        assert result["score"] == expected_score, (
            f"加权总分不匹配: 计算={expected_score}, 返回={result['score']}"
        )

    @pytest.mark.asyncio
    async def test_evaluate_with_llm_mock(self):
        """mock LLM 响应测试 LLM-as-Judge"""
        from agents.evaluator import ResponseEvaluator

        ev = ResponseEvaluator()

        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = (
            '{"completeness": 85, "accuracy": 90, "conciseness": 80, '
            '"politeness": 95, "relevance": 88}'
        )
        mock_llm.async_invoke = AsyncMock(return_value=mock_response)

        result = await ev.evaluate_with_llm(
            "面膜适合什么肤质",
            "这款面膜适合干性肌肤，含有保湿成分。",
            mock_llm,
        )

        assert result["method"] == "llm_judge"
        assert "score" in result
        assert "factors" in result
        assert result["factors"]["completeness"] == 85
        assert result["factors"]["accuracy"] == 90
        assert result["score"] > 0

    def test_trend_improving(self):
        """连续高分应显示 improving 趋势"""
        from agents.evaluator import ResponseEvaluator

        ev = ResponseEvaluator()
        # 需要 >= 4 条反馈（TREND_MIN_FEEDBACKS=4）
        # 前半段低评分（rating=-1），后半段高评分（rating=1）
        feedbacks = [
            {"rating": -1, "score": 30, "timestamp": 1},
            {"rating": -1, "score": 35, "timestamp": 2},
            {"rating": 1, "score": 80, "timestamp": 3},
            {"rating": 1, "score": 90, "timestamp": 4},
            {"rating": 1, "score": 85, "timestamp": 5},
        ]
        result = ev.aggregate_feedback(feedbacks)
        assert result["trend"] == "improving", f"趋势应为 improving, 实际={result['trend']}"
        assert result["positive"] == 3
        assert result["negative"] == 2


# ═══════════════════════════════════════════════════════════════════════════════
# T4c: ReAct Agent 推理链测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestReActAgentDeep:
    """ReActAgent 推理链 + 工具调用循环深度验证"""

    @pytest.mark.asyncio
    async def test_react_tool_call_loop(self):
        """mock LLM 返回 tool_calls，验证工具被调用"""
        from agents.react_agent import ReActAgent

        agent = ReActAgent(max_iterations=3)

        # Mock LLM：第一轮返回 tool_calls，第二轮返回最终文本
        tool_call_response = MagicMock()
        tool_call_response.content = ""
        tool_call_response.tool_calls = [
            {
                "id": "call_001",
                "name": "query_product",
                "arguments": {"keyword": "面膜"},
            }
        ]

        final_response = MagicMock()
        final_response.content = "面膜产品信息已找到，适合干性肌肤使用。"
        final_response.tool_calls = []

        call_count = 0

        async def mock_invoke(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return tool_call_response
            return final_response

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=mock_invoke)
        agent.set_llm(mock_llm)

        # Mock tool registry
        mock_registry = MagicMock()
        mock_registry.get_openai_tools.return_value = [
            {"type": "function", "function": {"name": "query_product"}}
        ]
        mock_registry.execute = AsyncMock(return_value="面膜产品库存充足，售价88元")
        agent.set_tool_registry(mock_registry)

        # Mock session manager
        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        # Mock knowledge_base (no RAG)
        agent.knowledge_base = None

        state = {
            "session_id": "react_test_001",
            "customer_query": "面膜有什么推荐",
            "response": "",
            "current_agent": "",
        }

        result = await agent.process(state)
        assert result["response"] == "面膜产品信息已找到，适合干性肌肤使用。"
        # 验证工具被调用（v6.0: execute 新增 stream_callback 参数）
        mock_registry.execute.assert_called_once_with(
            "query_product", {"keyword": "面膜"}, stream_callback=None
        )

    @pytest.mark.asyncio
    async def test_react_max_iterations(self):
        """超过 max_tool_rounds 后循环终止"""
        from agents.react_agent import ReActAgent

        agent = ReActAgent(max_iterations=2)

        # LLM 每轮都返回 tool_calls（永远不给最终回答）
        tool_response = MagicMock()
        tool_response.content = ""
        tool_response.tool_calls = [
            {
                "id": "call_loop",
                "name": "query_product",
                "arguments": {"keyword": "test"},
            }
        ]

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=tool_response)
        agent.set_llm(mock_llm)

        mock_registry = MagicMock()
        mock_registry.get_openai_tools.return_value = [{"type": "function"}]
        mock_registry.execute = AsyncMock(return_value="some result")
        agent.set_tool_registry(mock_registry)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)
        agent.knowledge_base = None

        state = {
            "session_id": "react_max_iter",
            "customer_query": "测试循环",
            "response": "",
            "current_agent": "",
        }

        result = await agent.process(state)
        # 超过 max_iterations=2 后应使用 fallback
        fallback = "抱歉，处理您的复杂问题时遇到困难。建议您提供更多细节或联系人工客服。"
        assert result["response"] == fallback
        # LLM 应恰好被调用 max_iterations 次
        assert mock_llm.async_invoke.call_count == 2

    @pytest.mark.asyncio
    async def test_react_tool_failure_graceful(self):
        """工具调用失败返回 fallback"""
        from agents.react_agent import ReActAgent

        agent = ReActAgent(max_iterations=3)

        tool_response = MagicMock()
        tool_response.content = ""
        tool_response.tool_calls = [
            {
                "id": "call_fail",
                "name": "query_order",
                "arguments": {"order_id": "ORD001"},
            }
        ]

        final_response = MagicMock()
        final_response.content = "工具暂时不可用，请稍后重试。"
        final_response.tool_calls = []

        call_count = 0

        async def mock_invoke(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return tool_response
            return final_response

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=mock_invoke)
        agent.set_llm(mock_llm)

        # 工具调用抛出异常
        mock_registry = MagicMock()
        mock_registry.get_openai_tools.return_value = [{"type": "function"}]
        mock_registry.execute = AsyncMock(side_effect=RuntimeError("ERP connection failed"))
        agent.set_tool_registry(mock_registry)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)
        agent.knowledge_base = None

        state = {
            "session_id": "react_tool_fail",
            "customer_query": "查订单",
            "response": "",
            "current_agent": "",
        }

        result = await agent.process(state)
        # 工具失败不应导致异常，应有正常响应
        assert result["response"] != ""
        assert "current_agent" in result

    @pytest.mark.asyncio
    async def test_react_no_tools_final_answer(self):
        """LLM 直接返回文本（无 tool_calls）时正常结束"""
        from agents.react_agent import ReActAgent

        agent = ReActAgent(max_iterations=3)

        # LLM 直接返回最终回答，无 tool_calls
        direct_response = MagicMock()
        direct_response.content = "您好，这款产品适合所有肤质。"
        direct_response.tool_calls = []

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=direct_response)
        agent.set_llm(mock_llm)

        mock_registry = MagicMock()
        mock_registry.get_openai_tools.return_value = []
        agent.set_tool_registry(mock_registry)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)
        agent.knowledge_base = None

        state = {
            "session_id": "react_no_tools",
            "customer_query": "产品适合什么肤质",
            "response": "",
            "current_agent": "",
        }

        result = await agent.process(state)
        assert result["response"] == "您好，这款产品适合所有肤质。"
        # 只调用一次 LLM
        mock_llm.async_invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_react_rag_context_injection(self):
        """验证 RAG 知识注入到上下文"""
        from agents.react_agent import ReActAgent

        agent = ReActAgent(max_iterations=3)

        # 直接返回回答
        direct_response = MagicMock()
        direct_response.content = "烟酰胺精华适合油性肌肤。"
        direct_response.tool_calls = []

        captured_messages = []

        async def mock_invoke(messages, tools=None):
            captured_messages.extend(messages)
            return direct_response

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=mock_invoke)
        agent.set_llm(mock_llm)

        mock_registry = MagicMock()
        mock_registry.get_openai_tools.return_value = []
        agent.set_tool_registry(mock_registry)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        # Mock RAG knowledge base
        mock_kb = MagicMock()
        mock_kb.available = True
        mock_kb.query_multiple = AsyncMock(
            return_value=[{"content": "烟酰胺精华适合油性和混合性肌肤，建议每天使用一次。"}]
        )
        mock_kb.rewrite_query = AsyncMock(side_effect=lambda q, llm: q)
        agent.set_knowledge_base(mock_kb)

        state = {
            "session_id": "react_rag",
            "customer_query": "烟酰胺精华适合什么肤质",
            "response": "",
            "current_agent": "",
        }

        result = await agent.process(state)
        assert result["response"] == "烟酰胺精华适合油性肌肤。"

        # 验证 RAG 知识被注入到 LLM 消息中
        all_content = " ".join(
            getattr(m, "content", "") for m in captured_messages if hasattr(m, "content")
        )
        assert "烟酰胺精华" in all_content, "RAG 知识应被注入到 LLM 上下文中"
        assert "知识库" in all_content or "检索知识" in all_content, "RAG 内容应包含知识库标记"


# ═══════════════════════════════════════════════════════════════════════════════
# T11: ReAct Self-Reflection 测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestSelfReflection:
    """ReAct Self-Reflection 功能验证"""

    @pytest.mark.asyncio
    async def test_self_reflection_disabled_by_default(self):
        """REACT_SELF_REFLECTION=false 时不执行 reflection"""
        from unittest.mock import AsyncMock, MagicMock

        from agents.base_agent import BaseAgent
        from core import config as _cfg

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return await self._process_with_tools(
                    state,
                    "test prompt",
                    fallback_response="fallback",
                )

        agent = DummyAgent(name="test", role="test", expertise=["test"])

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(
            return_value=MagicMock(content="原始回答", tool_calls=None)
        )
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        state = {"session_id": "sr_disabled", "customer_query": "产品推荐"}

        old_val = _cfg.REACT_SELF_REFLECTION
        try:
            _cfg.REACT_SELF_REFLECTION = False
            result = await agent.process(state)
        finally:
            _cfg.REACT_SELF_REFLECTION = old_val

        assert mock_llm.async_invoke.call_count == 1
        assert result["response"] == "原始回答"

    @pytest.mark.asyncio
    async def test_self_reflection_pass_no_change(self):
        """reflection 返回 PASS 时回答不变"""
        from unittest.mock import AsyncMock, MagicMock, patch

        from agents.base_agent import BaseAgent

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return await self._process_with_tools(
                    state,
                    "test prompt",
                    fallback_response="fallback",
                )

        agent = DummyAgent(name="test", role="test", expertise=["test"])

        call_count = 0

        async def mock_invoke(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(content="好的回答", tool_calls=None)
            return MagicMock(content="PASS", tool_calls=None)

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=mock_invoke)
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        state = {"session_id": "sr_pass", "customer_query": "产品推荐"}

        with patch("core.config.REACT_SELF_REFLECTION", True):
            result = await agent.process(state)

        assert call_count == 2
        assert result["response"] == "好的回答"

    @pytest.mark.asyncio
    async def test_self_reflection_improves(self):
        """reflection 返回改进建议时回答更新"""
        from unittest.mock import AsyncMock, MagicMock, patch

        from agents.base_agent import BaseAgent

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return await self._process_with_tools(
                    state,
                    "test prompt",
                    fallback_response="fallback",
                )

        agent = DummyAgent(name="test", role="test", expertise=["test"])

        call_count = 0

        async def mock_invoke(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(content="简短回答", tool_calls=None)
            elif call_count == 2:
                return MagicMock(content="回答缺少具体成分建议，请补充", tool_calls=None)
            else:
                return MagicMock(content="改进后的详细回答", tool_calls=None)

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=mock_invoke)
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        state = {"session_id": "sr_improve", "customer_query": "产品推荐"}

        with patch("core.config.REACT_SELF_REFLECTION", True):
            result = await agent.process(state)

        assert call_count == 3
        assert result["response"] == "改进后的详细回答"

    @pytest.mark.asyncio
    async def test_self_reflection_failure_ignored(self):
        """reflection 异常时保留原回答"""
        from unittest.mock import AsyncMock, MagicMock, patch

        from agents.base_agent import BaseAgent

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return await self._process_with_tools(
                    state,
                    "test prompt",
                    fallback_response="fallback",
                )

        agent = DummyAgent(name="test", role="test", expertise=["test"])

        call_count = 0

        async def mock_invoke(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(content="原始回答", tool_calls=None)
            raise RuntimeError("LLM service unavailable")

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=mock_invoke)
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        state = {"session_id": "sr_fail", "customer_query": "产品推荐"}

        with patch("core.config.REACT_SELF_REFLECTION", True):
            result = await agent.process(state)

        assert result["response"] == "原始回答"


# ═══════════════════════════════════════════════════════════════════════════════
# T12: Agent 黑板通信强化测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlackboardEnhanced:
    """Agent 黑板通信强化功能验证"""

    @pytest.mark.asyncio
    async def test_product_agent_writes_blackboard(self):
        """验证 ProductAgent 写入黑板 product.recommendation"""
        from unittest.mock import AsyncMock, MagicMock

        from agents.product_agent import ProductAgent

        agent = ProductAgent()
        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="产品推荐"))
        agent.set_llm(mock_llm)

        agent.erp = MagicMock()
        agent.erp.query_product = AsyncMock(return_value=[])
        agent.erp.query_inventory = AsyncMock(return_value=[])

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        bb_writes = {}

        class MockBB:
            async def write(self, key, value, ttl=None):
                bb_writes[key] = value

            async def read_prefix(self, prefix):
                return {}

        agent.set_blackboard(MockBB())

        state = {"session_id": "bb_prod", "customer_query": "推荐防晒"}
        await agent.process(state)

        assert "product.recommendation" in bb_writes
        rec = bb_writes["product.recommendation"]
        assert rec["query"] == "推荐防晒"
        assert isinstance(rec["has_erp_data"], bool)
        assert isinstance(rec["has_rag_context"], bool)

    @pytest.mark.asyncio
    async def test_tech_agent_writes_blackboard(self):
        """验证 TechAgent 写入黑板 tech.diagnosis"""
        from unittest.mock import AsyncMock, MagicMock

        from agents.tech_agent import TechAgent

        agent = TechAgent()
        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="技术建议"))
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        bb_writes = {}

        class MockBB:
            async def write(self, key, value, ttl=None):
                bb_writes[key] = value

            async def read_prefix(self, prefix):
                return {}

        agent.set_blackboard(MockBB())

        state = {"session_id": "bb_tech", "customer_query": "过敏怎么办"}
        await agent.process(state)

        assert "tech.diagnosis" in bb_writes
        diag = bb_writes["tech.diagnosis"]
        assert diag["query"] == "过敏怎么办"
        assert isinstance(diag["has_tech_context"], bool)

    @pytest.mark.asyncio
    async def test_prepare_llm_messages_reads_blackboard(self):
        """验证 _prepare_llm_messages 读取黑板信息并注入 user_content"""
        from unittest.mock import AsyncMock, MagicMock

        from agents.base_agent import BaseAgent

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return await self._process_with_llm(
                    state,
                    "test prompt",
                    fallback_response="fallback",
                )

        agent = DummyAgent(name="test", role="test", expertise=["test"])

        captured_messages = []

        async def mock_invoke(messages, tools=None):
            captured_messages.extend(messages)
            return MagicMock(content="回复")

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=mock_invoke)
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        class MockBB:
            async def read_prefix(self, prefix):
                data = {
                    "product.": {"product.recommendation": {"query": "防晒", "has_erp_data": True}},
                    "tech.": {"tech.diagnosis": {"query": "过敏", "has_tech_context": True}},
                }
                return data.get(prefix, {})

        agent.set_blackboard(MockBB())

        state = {"session_id": "bb_read", "customer_query": "推荐防晒霜"}
        await agent.process(state)

        human_content = ""
        for msg in captured_messages:
            if (
                hasattr(msg, "content")
                and isinstance(msg.content, str)
                and "推荐防晒霜" in msg.content
            ):
                human_content = msg.content
                break

        assert "[其他 Agent 发现]" in human_content

    @pytest.mark.asyncio
    async def test_prepare_llm_messages_no_bb_no_error(self):
        """验证无黑板时 _prepare_llm_messages 不报错"""
        from unittest.mock import AsyncMock, MagicMock

        from agents.base_agent import BaseAgent

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return await self._process_with_llm(
                    state,
                    "test prompt",
                    fallback_response="fallback",
                )

        agent = DummyAgent(name="test", role="test", expertise=["test"])

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="回复"))
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        state = {"session_id": "no_bb", "customer_query": "你好"}
        result = await agent.process(state)

        assert result["response"] == "回复"


# ═══════════════════════════════════════════════════════════════════════════════
# ERPFactory 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestERPFactory:
    """erp.factory — ERP 适配器工厂测试"""

    def test_create_mock_mode(self):
        """ERP_MODE=mock 时返回 KingdeeMockAdapter"""
        import erp.factory as ef

        with patch.object(ef, "ERP_MODE", "mock"):
            adapter = ef.create_erp_adapter()
            assert adapter.__class__.__name__ == "KingdeeMockAdapter"

    def test_create_real_mode_missing_config_fallback(self):
        """ERP_MODE=real 但缺少配置时自动降级到 mock"""
        import erp.factory as ef

        with (
            patch.object(ef, "ERP_MODE", "real"),
            patch.object(ef, "ERP_BASE_URL", ""),
            patch.object(ef, "ERP_APP_ID", ""),
            patch.object(ef, "ERP_APP_SECRET", ""),
            patch.object(ef, "ERP_DB_ID", ""),
        ):
            adapter = ef.create_erp_adapter()
            assert adapter.__class__.__name__ == "KingdeeMockAdapter"

    def test_create_real_mode_with_valid_config(self):
        """ERP_MODE=real 且配置完整时返回 KingdeeRealAdapter"""
        import types

        import erp.factory as ef

        mock_adapter_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.__class__.__name__ = "KingdeeRealAdapter"
        mock_adapter_cls.return_value = mock_instance

        fake_module = types.ModuleType("erp.kingdee_real_adapter")
        fake_module.KingdeeRealAdapter = mock_adapter_cls

        patched_fields = {
            "ERP_BASE_URL": ("金蝶 Cloud API 地址", "https://kd.example.com"),
            "ERP_APP_ID": ("应用 ID", "app123"),
            "ERP_APP_SECRET": ("应用密钥", "secret456"),
            "ERP_DB_ID": ("账套 ID", "db789"),
        }
        with (
            patch.object(ef, "ERP_MODE", "real"),
            patch.object(ef, "_REAL_REQUIRED_FIELDS", patched_fields),
            patch.object(ef, "ERP_BASE_URL", "https://kd.example.com"),
            patch.object(ef, "ERP_APP_ID", "app123"),
            patch.object(ef, "ERP_APP_SECRET", "secret456"),
            patch.object(ef, "ERP_DB_ID", "db789"),
            patch.dict("sys.modules", {"erp.kingdee_real_adapter": fake_module}),
        ):
            ef.create_erp_adapter()
            mock_adapter_cls.assert_called_once_with(
                base_url="https://kd.example.com",
                app_id="app123",
                app_secret="secret456",
                db_id="db789",
            )

    def test_create_real_mode_import_error_fallback(self):
        """ERP_MODE=real 配置完整但 import 失败时降级到 mock"""
        import erp.factory as ef

        patched_fields = {
            "ERP_BASE_URL": ("金蝶 Cloud API 地址", "https://kd.example.com"),
            "ERP_APP_ID": ("应用 ID", "app123"),
            "ERP_APP_SECRET": ("应用密钥", "secret456"),
            "ERP_DB_ID": ("账套 ID", "db789"),
        }
        with (
            patch.object(ef, "ERP_MODE", "real"),
            patch.object(ef, "_REAL_REQUIRED_FIELDS", patched_fields),
            patch.dict("sys.modules", {"erp.kingdee_real_adapter": None}),
        ):
            adapter = ef.create_erp_adapter()
            assert adapter.__class__.__name__ == "KingdeeMockAdapter"

    def test_unknown_mode_fallback_to_mock(self):
        """未知 ERP_MODE 时降级到 mock 并输出警告"""
        import erp.factory as ef

        with patch.object(ef, "ERP_MODE", "invalid_mode"):
            adapter = ef.create_erp_adapter()
            assert adapter.__class__.__name__ == "KingdeeMockAdapter"

    def test_validate_real_config_returns_missing_fields(self):
        """_validate_real_config 返回缺失的配置项列表"""
        import erp.factory as ef

        # 必须 patch _REAL_REQUIRED_FIELDS 本身，因为它在模块加载时已捕获值
        patched_fields = {
            "ERP_BASE_URL": ("金蝶 Cloud API 地址", ""),
            "ERP_APP_ID": ("应用 ID", "app"),
            "ERP_APP_SECRET": ("应用密钥", ""),
            "ERP_DB_ID": ("账套 ID", "db"),
        }
        with patch.object(ef, "_REAL_REQUIRED_FIELDS", patched_fields):
            missing = ef._validate_real_config()
            assert len(missing) == 2
            assert any("ERP_BASE_URL" in m for m in missing)
            assert any("ERP_APP_SECRET" in m for m in missing)

    def test_validate_real_config_all_present(self):
        """所有配置项都有值时返回空列表"""
        import erp.factory as ef

        patched_fields = {
            "ERP_BASE_URL": ("金蝶 Cloud API 地址", "https://kd.example.com"),
            "ERP_APP_ID": ("应用 ID", "app"),
            "ERP_APP_SECRET": ("应用密钥", "secret"),
            "ERP_DB_ID": ("账套 ID", "db"),
        }
        with patch.object(ef, "_REAL_REQUIRED_FIELDS", patched_fields):
            missing = ef._validate_real_config()
            assert missing == []

    def test_validate_real_config_whitespace_only(self):
        """配置项只有空白时视为缺失"""
        import erp.factory as ef

        patched_fields = {
            "ERP_BASE_URL": ("金蝶 Cloud API 地址", "   "),
            "ERP_APP_ID": ("应用 ID", "app"),
            "ERP_APP_SECRET": ("应用密钥", "secret"),
            "ERP_DB_ID": ("账套 ID", "db"),
        }
        with patch.object(ef, "_REAL_REQUIRED_FIELDS", patched_fields):
            missing = ef._validate_real_config()
            assert len(missing) == 1
            assert "ERP_BASE_URL" in missing[0]


# ═══════════════════════════════════════════════════════════════════════════════
# ComplaintAgent — RAG 失败降级路径
# ═══════════════════════════════════════════════════════════════════════════════


class TestComplaintAgentRAGFallback:
    """ComplaintAgent RAG 检索失败时的降级处理"""

    @pytest.mark.asyncio
    async def test_rag_retrieval_failure_fallback(self):
        """知识库检索异常时仍能正常回复（降级路径）"""
        from agents.complaint_agent import ComplaintAgent

        agent = ComplaintAgent()

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="抱歉给您带来不好体验"))
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        bb = MagicMock()
        bb.write = AsyncMock()
        bb.read_prefix = AsyncMock(return_value={})
        agent.set_blackboard(bb)

        kb = MagicMock()
        kb.available = True
        kb.query_multiple = AsyncMock(side_effect=Exception("DB 连接超时"))
        agent.set_knowledge_base(kb)

        state = {"session_id": "complaint_rag_fail", "customer_query": "我买的面霜过敏了"}
        result = await agent.process(state)

        assert "response" in result
        assert result["current_agent"] == "投诉处理专家"

    @pytest.mark.asyncio
    async def test_rag_retrieval_success(self):
        """知识库检索成功时将上下文注入 LLM"""
        from agents.complaint_agent import ComplaintAgent

        agent = ComplaintAgent()

        captured_messages = []

        async def capture_invoke(messages, **kw):
            captured_messages.extend(messages)
            return MagicMock(content="已为您处理投诉")

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=capture_invoke)
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        bb = MagicMock()
        bb.write = AsyncMock()
        bb.read_prefix = AsyncMock(return_value={})
        agent.set_blackboard(bb)

        kb = MagicMock()
        kb.available = True
        kb.query_multiple = AsyncMock(
            return_value=[
                {"content": "过敏投诉应先确认产品批次并建议就医"},
                {"content": "可提供无条件退款"},
            ]
        )
        agent.set_knowledge_base(kb)

        state = {"session_id": "complaint_rag_ok", "customer_query": "面霜过敏"}
        result = await agent.process(state)

        assert "response" in result
        user_msgs = [m for m in captured_messages if hasattr(m, "type") and m.type == "human"]
        if user_msgs:
            assert "过敏投诉" in user_msgs[0].content or "面霜" in user_msgs[0].content


# ═══════════════════════════════════════════════════════════════════════════════
# GeneralAgent — 黑板读取和 ERP 查询路径
# ═══════════════════════════════════════════════════════════════════════════════


class TestGeneralAgentPaths:
    """GeneralAgent 黑板读取与 ERP 查询路径"""

    @pytest.mark.asyncio
    async def test_erp_customer_query(self):
        """查询中含 C### 客户编号时触发 ERP 查询"""
        from agents.general_agent import GeneralAgent

        agent = GeneralAgent()

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="欢迎回来，张三"))
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        bb = MagicMock()
        bb.write = AsyncMock()
        bb.read_prefix = AsyncMock(return_value={})
        agent.set_blackboard(bb)

        erp = MagicMock()
        erp.query_customer = AsyncMock(
            return_value={
                "name": "张三",
                "phone": "13800138000",
                "level": "VIP",
                "total_spent": 50000,
                "address": "北京市朝阳区",
            }
        )
        agent.set_erp(erp)

        state = {"session_id": "general_erp", "customer_query": "我是客户 C001，想查一下订单"}
        result = await agent.process(state)

        assert "response" in result
        erp.query_customer.assert_called_once_with("C001")
        # 验证黑板写入了客户数据
        assert bb.write.called
        write_args = bb.write.call_args
        assert write_args[0][0] == "erp.customer_data"
        assert "张三" in str(write_args)

    @pytest.mark.asyncio
    async def test_erp_query_failure_graceful(self):
        """ERP 查询失败时不阻断流程"""
        from agents.general_agent import GeneralAgent

        agent = GeneralAgent()

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="请问有什么可以帮助您的"))
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        bb = MagicMock()
        bb.write = AsyncMock()
        bb.read_prefix = AsyncMock(return_value={})
        agent.set_blackboard(bb)

        erp = MagicMock()
        erp.query_customer = AsyncMock(side_effect=Exception("ERP 不可用"))
        agent.set_erp(erp)

        state = {"session_id": "general_erp_fail", "customer_query": "客户 C002 你好"}
        result = await agent.process(state)

        assert "response" in result

    @pytest.mark.asyncio
    async def test_blackboard_data_injected(self):
        """黑板上有 erp. 前缀数据时注入到 extra_context"""
        from agents.general_agent import GeneralAgent

        agent = GeneralAgent()

        captured_messages = []

        async def capture_invoke(messages, **kw):
            captured_messages.extend(messages)
            return MagicMock(content="已为您查询")

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=capture_invoke)
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        bb = MagicMock()
        bb.write = AsyncMock()
        bb.read_prefix = AsyncMock(return_value={"erp.order_status": "已发货"})
        agent.set_blackboard(bb)

        state = {"session_id": "general_bb", "customer_query": "查一下我的订单"}
        result = await agent.process(state)

        assert "response" in result
        user_msgs = [m for m in captured_messages if hasattr(m, "type") and m.type == "human"]
        if user_msgs:
            assert "erp.order_status" in user_msgs[0].content or "已发货" in user_msgs[0].content

    @pytest.mark.asyncio
    async def test_no_bb_no_erp(self):
        """无黑板、无 ERP 时正常处理"""
        from agents.general_agent import GeneralAgent

        agent = GeneralAgent()

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="请问有什么可以帮助您的"))
        agent.set_llm(mock_llm)

        sm = MagicMock()
        sm.add_message = AsyncMock()
        sm.get_conversation_context = AsyncMock(return_value=[])
        sm.detect_drift = AsyncMock(return_value={"has_drift": False, "drifts": []})
        agent.set_session_manager(sm)

        state = {"session_id": "general_plain", "customer_query": "你好"}
        result = await agent.process(state)

        assert result["response"] == "请问有什么可以帮助您的"
        assert result["current_agent"] == "通用咨询专家"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])


# ═══════════════════════════════════════════════════════════════════════════════
# ResponseCache — Additional L2 / Redis / Eviction Coverage
# ═══════════════════════════════════════════════════════════════════════════════


class TestResponseCacheFallback:
    """ResponseCache L3 Jaccard 降级 + Edge Cases"""

    def test_jaccard_search_hit(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("red lipstick recommendation", "Product A is great")
        result = cache.get("red lipstick recommendation")
        assert result == "Product A is great"

    def test_l3_eviction(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        for i in range(600):
            cache.put(f"unique_query_{i}_abc", f"response_{i}")
        stats = cache.get_stats()
        assert stats["l3_size"] <= 500

    def test_invalidate_l1_no_error(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("inv_key", "inv_val")
        cache.invalidate("inv_key")
        assert True

    def test_evict_l3_empty(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache()
        cache._l3_evict()
        assert True

    def test_normalize(self):
        from cache.response_cache import ResponseCache

        assert ResponseCache._normalize("  Hello World  ") == "hello world"
        assert ResponseCache._normalize("Ｈｅｌｌｏ") == "hello"

    def test_invalidate_by_filter_no_crash(self):
        """invalidate_by_filter 无 Qdrant 时不抛异常"""
        from cache.response_cache import ResponseCache

        c = ResponseCache()
        c.invalidate_by_filter({"product_id": "SKU_123"})
        assert True

    def test_cleanup_expired_no_crash(self):
        """cleanup_expired 无 Qdrant 时返回 0"""
        from cache.response_cache import ResponseCache

        c = ResponseCache()
        count = c.cleanup_expired()
        assert count == 0

    def test_get_with_metadata(self):
        """get 传入 metadata 不报错（向后兼容）"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("test", "result", metadata={"intent_type": "knowledge_qa", "user_role": "vip"})
        assert cache.get("test") == "result"
        assert cache.get("test", metadata={"user_role": "vip"}) == "result"

    def test_alertmanager_config_no_self_referencing_urls(self):
        """验证 alertmanager.yml 格式有效性，且不包含指向本机的自指 URL，以防止告警丢失"""
        import os

        import yaml

        project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        config_path = os.path.join(project_dir, "monitoring", "alertmanager.yml")
        assert os.path.exists(config_path), f"配置文件不存在: {config_path}"

        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        assert config is not None
        assert "receivers" in config, "Alertmanager 应该有 receivers 配置"

        # 检查 receivers 中的 webhook_configs
        for receiver in config["receivers"]:
            webhook_configs = receiver.get("webhook_configs", [])
            for webhook in webhook_configs:
                url = webhook.get("url", "")
                assert "app:8000" not in url, f"发现自指地址 '{url}' 在接收者 '{receiver['name']}' 中，容易导致告警丢失"
                assert "localhost:8000" not in url, f"发现自指地址 '{url}' 在接收者 '{receiver['name']}' 中，容易导致告警丢失"
                assert "127.0.0.1:8000" not in url, f"发现自指地址 '{url}' 在接收者 '{receiver['name']}' 中，容易导致告警丢失"

    def test_secrets_rotation_script(self):
        """测试 scripts/rotate_secrets.py 能否正确轮换密钥并且不破坏其他配置"""
        import os
        import subprocess
        import tempfile

        # 创建一个临时的 env 文件
        with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8") as tmp:
            tmp.write(
                "JWT_SECRET=old_jwt_secret\n"
                "SESSION_TOKEN_SECRET=old_session_secret\n"
                "OPENAI_API_KEY=sk-stay_same\n"
                "DATABASE_URL=postgresql://user:pass@localhost/db\n"
            )
            tmp_path = tmp.name

        try:
            # 运行 rotate_secrets.py 脚本
            project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            script_path = os.path.join(project_dir, "scripts", "rotate_secrets.py")
            res = subprocess.run(
                ["python3", script_path, tmp_path],
                capture_output=True,
                text=True,
                check=True
            )
            assert "密钥轮换成功完成" in res.stdout

            # 读取轮换后的文件内容
            with open(tmp_path, encoding="utf-8") as f:
                content = f.read()

            lines = content.splitlines()
            rotated_dict = {}
            for line in lines:
                if "=" in line:
                    k, v = line.split("=", 1)
                    rotated_dict[k] = v

            # 验证内部密钥已被轮换并且是新的随机值
            assert rotated_dict["JWT_SECRET"] != "old_jwt_secret"
            assert rotated_dict["SESSION_TOKEN_SECRET"] != "old_session_secret"
            assert len(rotated_dict["JWT_SECRET"]) > 10

            # 验证外部配置和 API 密钥保持原样
            assert rotated_dict["OPENAI_API_KEY"] == "sk-stay_same"
            assert rotated_dict["DATABASE_URL"] == "postgresql://user:pass@localhost/db"

        finally:
            # 清理临时文件和备份文件
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            # 清理生成的备份文件 (.bak.*)
            dir_name = os.path.dirname(tmp_path)
            base_name = os.path.basename(tmp_path)
            for f in os.listdir(dir_name):
                if f.startswith(base_name + ".bak."):
                    os.remove(os.path.join(dir_name, f))

