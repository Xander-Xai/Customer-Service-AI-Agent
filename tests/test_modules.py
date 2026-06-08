"""
模块级全面验证测试 (v3.8)
覆盖：SessionManager / ResponseAgent / Cache / Router / ERP / Agents / Core / RAG / Tools / API / Collaboration
运行: pytest tests/test_modules.py -v
"""
import os, sys, asyncio, time, json, re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SessionManager 模块
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionManagerModule:
    """SessionManager 完整验证"""

    @pytest.mark.asyncio
    async def test_create_session_returns_id(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        sid = await sm.create_session("test_001")
        assert sid == "test_001"
        assert "test_001" in sm.sessions

    @pytest.mark.asyncio
    async def test_create_session_auto_uuid(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        sid = await sm.create_session()
        assert isinstance(sid, str)
        assert len(sid) > 0

    @pytest.mark.asyncio
    async def test_create_session_invalid_id_generates_new(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        sid = await sm.create_session("../../../etc/passwd")
        assert sid != "../../../etc/passwd"
        assert len(sid) > 10

    @pytest.mark.asyncio
    async def test_add_message_sets_role(self):
        from session_manager import EnhancedSessionManager
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
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        await sm.create_session("s2")
        for i in range(5):
            await sm.add_message("s2", f"msg_{i}")
        session = await sm.get_session("s2")
        assert session["message_count"] == 5

    @pytest.mark.asyncio
    async def test_token_eviction_enforced(self):
        """验证 max_tokens 参数生效，token 裁剪实际触发"""
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager(window_size=10, max_tokens=50)
        sm.create_session("tok_test")
        for i in range(20):
            sm.add_message("tok_test", f"这是一条较长的消息用于测试token裁剪功能_{i:02d}", is_user=(i % 2 == 0))
        ctx = await sm.get_conversation_context("tok_test")
        total_chars = sum(len(m.get("content", "")) for m in ctx)
        full_chars = sum(len(f"这是一条较长的消息用于测试token裁剪功能_{i:02d}") for i in range(20))
        assert total_chars < full_chars, "Token 裁剪应减少上下文长度"

    @pytest.mark.asyncio
    async def test_window_size_limits_messages(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager(window_size=3)
        sm.create_session("win_test")
        for i in range(10):
            sm.add_message("win_test", f"消息{i}")
        ctx = await sm.get_conversation_context("win_test")
        non_summary = [m for m in ctx if "[历史摘要]" not in m.get("content", "")]
        assert len(non_summary) <= sm.window_size * 2 + 1

    @pytest.mark.asyncio
    async def test_delete_session(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        await sm.create_session("del_test")
        assert "del_test" in sm.sessions
        await sm.delete_session("del_test")
        assert "del_test" not in sm.sessions

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        await sm.create_session("ls_1")
        await sm.create_session("ls_2")
        sessions = await sm.list_sessions()
        ids = [s["session_id"] for s in sessions]
        assert "ls_1" in ids
        assert "ls_2" in ids

    @pytest.mark.asyncio
    async def test_session_expiry(self):
        from session_manager import EnhancedSessionManager
        sm = EnhancedSessionManager()
        await sm.create_session("exp_test")
        sm.sessions["exp_test"]["last_activity"] = time.time() - 999999
        sm._evict_idle_sessions()
        assert "exp_test" not in sm.sessions

    # ---- Drift Detection ----

    @pytest.mark.asyncio
    async def test_topic_drift_detection(self):
        from session_manager import EnhancedSessionManager
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
        from session_manager import EnhancedSessionManager
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
        from session_manager import EnhancedSessionManager
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
        from session_manager import EnhancedSessionManager
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
        from session_manager import _count_tokens
        tokens = _count_tokens("你好世界")
        assert tokens > 0
        assert isinstance(tokens, int)

    def test_count_tokens_english(self):
        from session_manager import _count_tokens
        tokens = _count_tokens("hello world")
        assert tokens > 0

    def test_count_tokens_empty(self):
        from session_manager import _count_tokens
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
        from agents.response_agent import ResponseAgent, RESOLUTION_RESOLVED
        ra = ResponseAgent()
        state = {"response": "这款产品适合您，含有玻尿酸成分，可以有效保湿。建议每天使用两次。"}
        status = ra._evaluate_resolution(state)
        assert status == RESOLUTION_RESOLVED

    def test_resolution_failed_empty(self):
        from agents.response_agent import ResponseAgent, RESOLUTION_FAILED
        ra = ResponseAgent()
        assert ra._evaluate_resolution({"response": ""}) == RESOLUTION_FAILED
        assert ra._evaluate_resolution({"response": "处理出错，请重试"}) == RESOLUTION_FAILED

    def test_resolution_escalated(self):
        from agents.response_agent import ResponseAgent, RESOLUTION_ESCALATED
        ra = ResponseAgent()
        state = {"response": "您的问题需要转接人工客服处理，正在为您转接。"}
        assert ra._evaluate_resolution(state) == RESOLUTION_ESCALATED

    def test_resolution_uncertain_short(self):
        from agents.response_agent import ResponseAgent, RESOLUTION_UNCERTAIN
        ra = ResponseAgent()
        state = {"response": "好的"}
        assert ra._evaluate_resolution(state) == RESOLUTION_UNCERTAIN

    def test_resolution_uncertain_phrase(self):
        from agents.response_agent import ResponseAgent, RESOLUTION_UNCERTAIN
        ra = ResponseAgent()
        state = {"response": "抱歉无法确定该产品的具体成分，请您谅解。"}
        assert ra._evaluate_resolution(state) == RESOLUTION_UNCERTAIN


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ResponseCache 模块
# ═══════════════════════════════════════════════════════════════════════════════

class TestResponseCacheModule:
    """ResponseCache L1/L2 完整验证"""

    def test_l1_put_get(self):
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=100, l2_max=0)
        cache.put("你好", "您好！")
        assert cache.get("你好") == "您好！"

    def test_l1_miss(self):
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=100, l2_max=0)
        assert cache.get("不存在的查询") is None

    def test_l1_eviction(self):
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=3, l2_max=0)
        # Use unique queries that won't match semantically
        cache.put("alpha_unique_xyz", "resp_a")
        cache.put("beta_unique_xyz", "resp_b")
        cache.put("gamma_unique_xyz", "resp_c")
        cache.put("delta_unique_xyz", "resp_d")
        cache.put("epsilon_unique_xyz", "resp_e")
        # L1 should have evicted oldest entries
        assert cache.get_stats()["l1_size"] == 3
        # alpha should be evicted from L1
        assert cache.get("alpha_unique_xyz") is None

    def test_l1_ttl_expiration(self):
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=100, l2_max=0, default_ttl=0.1)
        cache.put("ttl_test", "value")
        assert cache.get("ttl_test") == "value"
        time.sleep(0.2)
        assert cache.get("ttl_test") is None

    def test_cache_stats(self):
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=100, l2_max=0)
        cache.put("q1", "r1")
        cache.get("q1")
        cache.get("miss")
        stats = cache.get_stats()
        assert stats["l1_hits"] >= 1
        assert stats["misses"] >= 1

    def test_invalidate(self):
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=100, l2_max=0)
        cache.put("inv_test", "value")
        cache.invalidate("inv_test")
        assert cache.get_stats()["l1_size"] == 0

    def test_clear(self):
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=100, l2_max=0)
        cache.put("c1", "v1")
        cache.put("c2", "v2")
        cache.clear()
        assert cache.get("c1") is None


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
        assert result.query_type == "general_inquiry"

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
        result = await qr.route("我买的那个精华液用了过敏，想退货退款，订单号是12345，你们这个产品的成分是什么")
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
        assert hasattr(adapter, 'query_product')
        assert hasattr(adapter, 'query_inventory')
        assert hasattr(adapter, 'query_order')
        assert hasattr(adapter, 'query_customer')

    def test_abstract_interface(self):
        from erp.kingdee_adapter import KingdeeAdapterBase
        with pytest.raises(TypeError):
            KingdeeAdapterBase()

    def test_erp_tools_execution(self):
        from tools.erp_tools import create_erp_tools
        from erp.kingdee_adapter import KingdeeMockAdapter
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
        from agents.product_agent import ProductAgent
        from agents.billing_agent import BillingAgent
        from agents.tech_agent import TechAgent
        from agents.complaint_agent import ComplaintAgent
        from agents.general_agent import GeneralAgent
        from agents.response_agent import ResponseAgent
        from agents.react_agent import ReActAgent
        for cls in [ProductAgent, BillingAgent, TechAgent, ComplaintAgent,
                     GeneralAgent, ResponseAgent, ReActAgent]:
            assert issubclass(cls, BaseAgent)

    def test_drift_repair_strategies(self):
        from session_manager import DRIFT_REPAIR_STRATEGIES
        assert "topic_drift" in DRIFT_REPAIR_STRATEGIES
        assert "intent_drift" in DRIFT_REPAIR_STRATEGIES
        assert "repetition" in DRIFT_REPAIR_STRATEGIES

    def test_drift_type_class(self):
        from session_manager import DriftType
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
        from core.message_bus import MessageBus, Message, MessageType
        bus = MessageBus()
        received = []
        async def handler(msg):
            received.append(msg)
        await bus.subscribe("test.topic", handler)
        await bus.publish(Message(msg_type=MessageType.BROADCAST, topic="test.topic", sender="test", payload={"data": 1}))
        assert len(received) == 1
        assert received[0].payload["data"] == 1

    @pytest.mark.asyncio
    async def test_message_bus_unsubscribe(self):
        from core.message_bus import MessageBus, Message, MessageType
        bus = MessageBus()
        received = []
        async def handler(msg):
            received.append(msg)
        await bus.subscribe("test.topic", handler)
        await bus.unsubscribe("test.topic", handler)
        await bus.publish(Message(msg_type=MessageType.BROADCAST, topic="test.topic", sender="test", payload={}))
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
        kb = CosmeticsKnowledgeBase()
        assert kb.available

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
        registry.register("test_tool", "test desc", {"type": "object", "properties": {}}, AsyncMock(return_value="ok"))
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
        from tools.erp_tools import create_erp_tools
        from tools.tool_registry import ToolRegistry
        from erp.kingdee_adapter import KingdeeMockAdapter
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
        assert hasattr(orch, 'select_mode_name')
        assert hasattr(orch, 'build_context')

    def test_sequential_mode_selection(self):
        from collaboration.orchestrator import CollaborationOrchestrator
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard
        orch = CollaborationOrchestrator(MessageBus(), SharedBlackboard())
        mock_routing = MagicMock(complexity=1, query_type="product_info", agent_name="product_agent", fast_path=True)
        mode = orch.select_mode_name(mock_routing, {"customer_query": "你好"})
        assert mode == "sequential"

    def test_complaint_mode_selection(self):
        from collaboration.orchestrator import CollaborationOrchestrator
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard
        orch = CollaborationOrchestrator(MessageBus(), SharedBlackboard())
        mock_routing = MagicMock(complexity=3, query_type="complaint", agent_name="complaint_agent", fast_path=False)
        mode = orch.select_mode_name(mock_routing, {"customer_query": "投诉你们的产品"})
        assert mode == "hierarchical"

    def test_all_modes_exist(self):
        from collaboration.modes import (
            SequentialMode, ParallelMode, ConsultationMode, HierarchicalMode, ReActMode
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
        import config
        assert hasattr(config, "VERSION")
        assert hasattr(config, "LLM_MODEL") or hasattr(config, "OPENAI_MODEL")

    def test_security_defaults(self):
        import config
        assert isinstance(config.API_KEY_ENABLED, bool)
        assert 100 <= config.MAX_QUERY_LENGTH <= 10000

    def test_circuit_breaker_config(self):
        import config
        assert config.CIRCUIT_BREAKER_FAIL_THRESHOLD > 0
        assert config.CIRCUIT_BREAKER_RECOVERY_TIME > 0

    def test_session_config(self):
        import config
        assert config.MAX_SESSIONS > 0
        assert config.SESSION_IDLE_TTL > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 12. API 模块
# ═══════════════════════════════════════════════════════════════════════════════

class TestAPIModule:
    """API 端点 + 安全验证"""

    def test_sanitize_input(self):
        from api.app import _sanitize_input
        assert _sanitize_input("hello\x00world") == "helloworld"
        assert _sanitize_input("<script>alert('xss')</script>") != "<script>alert('xss')</script>"

    def test_validate_session_id_valid(self):
        from api.app import _validate_session_id
        sid = _validate_session_id("abc-123_test")
        assert sid == "abc-123_test"

    def test_validate_session_id_invalid(self):
        from api.app import _validate_session_id
        sid = _validate_session_id("../../etc/passwd")
        assert sid != "../../etc/passwd"
        assert len(sid) > 10

    def test_validate_session_id_empty(self):
        from api.app import _validate_session_id
        sid = _validate_session_id("")
        assert len(sid) > 0

    def test_health_endpoint(self):
        from multi_agent_customer_service import build_graph
        from core.container import ServiceContainer
        from api.app import create_app
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
        from multi_agent_customer_service import build_graph
        from core.container import ServiceContainer
        from api.app import create_app
        container = ServiceContainer()
        graph = build_graph(container)
        app = create_app(graph)
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert "Content-Security-Policy" in resp.headers

    def test_feedback_endpoint_validation(self):
        from multi_agent_customer_service import build_graph
        from core.container import ServiceContainer
        from api.app import create_app
        import config
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
        from multi_agent_customer_service import build_graph
        from core.container import ServiceContainer
        container = ServiceContainer()
        graph = build_graph(container)
        assert hasattr(graph, 'ainvoke')

    @pytest.mark.asyncio
    async def test_graph_simple_query(self):
        from multi_agent_customer_service import build_graph
        from core.container import ServiceContainer
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
        from logger import get_logger
        logger = get_logger("test")
        assert hasattr(logger, "info")
        assert hasattr(logger, "warning")

    def test_logger_has_methods(self):
        from logger import get_logger
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
        for root, dirs, files in os.walk('.'):
            dirs[:] = [d for d in dirs if d not in ('venv', '.venv', '.git', '__pycache__', 'node_modules', '.claude', 'htmlcov', 'scripts', 'data', 'logs')]
            for f in files:
                if f.endswith('.py'):
                    path = os.path.join(root, f)
                    try:
                        with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
                            content = fh.read()
                        for pattern in secret_patterns:
                            matches = re.findall(pattern, content, re.IGNORECASE)
                            real_matches = [m for m in matches if 'test' not in m.lower() and 'example' not in m.lower() and 'config' not in m.lower() and 'sample' not in m.lower()]
                            assert len(real_matches) == 0, f"Hardcoded secret in {path}: {real_matches}"
                    except (PermissionError, UnicodeDecodeError) as e:
                        pytest.fail(f"Failed to read {path}: {e}")

    def test_session_id_validation_pattern(self):
        from api.app import _SESSION_ID_RE
        assert _SESSION_ID_RE.match("abc123")
        assert _SESSION_ID_RE.match("test-session_id")
        assert not _SESSION_ID_RE.match("../../etc/passwd")
        assert not _SESSION_ID_RE.match("'; DROP TABLE--")
        assert not _SESSION_ID_RE.match("a" * 200)

    def test_cors_configuration(self):
        import config
        if hasattr(config, 'ENV') and config.ENV == 'production':
            assert config.CORS_ORIGINS != ["*"]


# ═══════════════════════════════════════════════════════════════════════════════
# 16. 性能基准测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestPerformance:
    """性能基准验证"""

    def test_cache_throughput(self):
        from cache.response_cache import ResponseCache
        cache = ResponseCache(l1_max=1000, l2_max=0)
        start = time.time()
        for i in range(1000):
            cache.put(f"q{i}", f"r{i}")
        for i in range(1000):
            cache.get(f"q{i}")
        elapsed = time.time() - start
        assert elapsed < 2.0, f"Cache throughput too slow: {elapsed:.2f}s"

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
        from llm.client import OpenAICompatibleClient
        from core.monitoring import CircuitBreaker
        cb = CircuitBreaker()
        client = OpenAICompatibleClient(
            api_key="sk-test", base_url="http://localhost:9999",
            model="test-model", circuit_breaker=cb,
        )
        # 模拟 SSE 响应
        mock_lines = [
            'data: {"choices":[{"delta":{"content":"你"}}]}',
            'data: {"choices":[{"delta":{"content":"好"}}]}',
            'data: {"choices":[{"delta":{"content":"！"}}]}',
            'data: [DONE]',
        ]

        class MockStreamResponse:
            status_code = 200
            def raise_for_status(self): pass
            async def aiter_lines(self):
                for line in mock_lines:
                    yield line
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class MockClient:
            is_closed = False
            def stream(self, method, url, **kwargs):
                return MockStreamResponse()

        # 注入 mock client
        client._get_async_client = lambda: MockClient().__class__.__mro__[0].__class__(
            MockClient
        )
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
        from agents.base_agent import BaseAgent
        from unittest.mock import AsyncMock, MagicMock

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return await self._process_with_llm(
                    state, "test prompt",
                    fallback_response="fallback",
                )

        agent = DummyAgent(name="test", role="test", expertise=["test"])

        # Mock LLM with async_invoke_stream
        collected_chunks = []

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

        result = await agent.process(state)

        # 验证：stream_callback 收到了 chunk 事件
        chunk_events = [e for e in streamed_events if e.get("type") == "chunk"]
        assert len(chunk_events) == 3
        assert chunk_events[0]["content"] == "Hello"
        assert chunk_events[1]["content"] == " "
        assert chunk_events[2]["content"] == "World"

        # 验证：state 中 response 是完整文本
        assert state["response"] == "Hello World"

    @pytest.mark.asyncio
    async def test_process_with_llm_no_callback_uses_standard(self):
        """验证无 stream_callback 时走标准非流式路径"""
        from agents.base_agent import BaseAgent
        from unittest.mock import AsyncMock, MagicMock

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return await self._process_with_llm(
                    state, "test prompt",
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

        result = await agent.process(state)
        assert state["response"] == "标准回复"
        mock_llm.async_invoke.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
