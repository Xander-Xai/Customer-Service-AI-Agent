"""
Mock LLM 集成测试（v3.9）
使用 Mock LLM 跑通完整 LangGraph 图流程，验证：
1. 端到端图调用（check_cache → classify → collaboration → final_response）
2. 缓存命中跳过路由
3. 5 种协作模式在不同查询下的路由
4. Agent.process() 真实调用路径
5. 会话上下文保持
6. 漂移检测触发
7. 错误降级处理

所有测试无需真实 LLM API Key，100% Mock。
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ===== Mock LLM 响应构造 =====

def _make_mock_llm(content: str = "这是一条测试回复", tool_calls=None):
    """构造一个行为可控的 Mock LLM 客户端"""
    mock = MagicMock()

    response = MagicMock()
    response.content = content
    response.tool_calls = tool_calls or []

    mock.async_invoke = AsyncMock(return_value=response)
    mock._response = response
    return mock


def _make_state(query: str, session_id: str = "test-session") -> dict:
    """构造初始图状态"""
    return {
        "session_id": session_id,
        "current_agent": "",
        "customer_query": query,
        "query_type": "",
        "response": "",
        "complexity": 0,
        "fast_path": False,
        "collaboration_mode": "",
        "cached": False,
        "agents_used": [],
        "resolution_status": "",
    }


# ===== 测试类 =====

class TestGraphEndToEnd:
    """端到端图调用测试（Mock LLM）"""

    @pytest.fixture(autouse=True)
    def setup(self):
        """每个测试前创建 Mock ServiceContainer"""
        from core.container import ServiceContainer
        from multi_agent_customer_service import build_graph

        # 创建 mock LLM
        self.mock_llm = _make_mock_llm("产品成分包含玻尿酸和烟酰胺，适合各种肤质。")

        # Patch OpenAICompatibleClient 避免真实 HTTP 调用
        self.patcher = patch(
            "llm.client.OpenAICompatibleClient",
            return_value=self.mock_llm,
        )
        self.patcher.start()

        # Patch ERP（create_erp_adapter 在 _init_agents 中通过 from erp.factory 导入）
        from erp.kingdee_adapter import KingdeeMockAdapter
        self.mock_erp = KingdeeMockAdapter()

        self.erp_patcher = patch(
            "erp.factory.create_erp_adapter",
            return_value=self.mock_erp,
        )
        self.erp_patcher.start()

        # 创建容器（同步部分），不调用 initialize()（避免真实 LLM 调用）
        self.container = ServiceContainer()

        # 手动注入 mock LLM
        self.container.llm = self.mock_llm

    def teardown_method(self):
        if hasattr(self, 'patcher'):
            self.patcher.stop()
        if hasattr(self, 'erp_patcher'):
            self.erp_patcher.stop()

    @pytest.mark.asyncio
    async def test_simple_query_end_to_end(self):
        """简单查询：缓存未命中 → 路由 → Sequential → 响应"""
        from multi_agent_customer_service import build_graph
        app = build_graph(self.container)
        state = _make_state("这款精华液多少钱？")

        result = await app.ainvoke(state)

        assert result["response"], "应有响应内容"
        assert result["cached"] is False
        assert result["query_type"] != ""
        assert result["collaboration_mode"] != ""
        assert result["current_agent"] != ""

    @pytest.mark.asyncio
    async def test_cache_hit_skips_routing(self):
        """缓存命中 → 直接跳到 final_response，跳过路由"""
        from multi_agent_customer_service import build_graph
        app = build_graph(self.container)

        # 先跑一次，让响应被缓存
        query = "产品保质期是多久？"
        state1 = _make_state(query, session_id="s1")
        result1 = await app.ainvoke(state1)
        assert result1["response"]

        # 再跑一次相同查询，应命中缓存
        state2 = _make_state(query, session_id="s2")
        result2 = await app.ainvoke(state2)

        assert result2["cached"] is True
        assert result2["query_type"] == ""  # 路由未执行
        assert result2["collaboration_mode"] == "cache_hit"

    @pytest.mark.asyncio
    async def test_complaint_query_end_to_end(self):
        """投诉查询 → 端到端走通（mock LLM 路由器可能返回非 complaint 分类，
        但图应仍能完整执行并返回响应。投诉→hierarchical 路由逻辑由 TestRoutingLogic 单独验证）"""
        from multi_agent_customer_service import build_graph
        app = build_graph(self.container)
        state = _make_state("我要投诉！你们的产品导致我皮肤过敏，要求退款赔偿！")

        result = await app.ainvoke(state)

        assert result["response"]
        assert result["collaboration_mode"]  # 应选择某种协作模式
        assert result["query_type"]  # 应完成路由

    @pytest.mark.asyncio
    async def test_multi_domain_query_end_to_end(self):
        """多领域查询 → 端到端走通（mock LLM 路由器分类有限，但图应完整执行。
        多领域→parallel/react 路由逻辑由 TestRoutingLogic 单独验证）"""
        from multi_agent_customer_service import build_graph
        app = build_graph(self.container)
        state = _make_state("我买了你们的精华液，想查一下订单物流，另外产品成分安全吗？")

        result = await app.ainvoke(state)

        assert result["response"]
        assert result["collaboration_mode"]
        assert result["query_type"]

    @pytest.mark.asyncio
    async def test_general_inquiry_sequential(self):
        """通用咨询 → Sequential 模式（使用唯一查询避免缓存干扰）"""
        from multi_agent_customer_service import build_graph
        app = build_graph(self.container)
        # 使用带时间戳的唯一查询，避免被之前的测试缓存命中
        import time
        unique_query = f"你好，请问有什么可以帮您的？{int(time.time() * 1000)}"
        state = _make_state(unique_query)

        result = await app.ainvoke(state)

        assert result["response"]
        assert result["collaboration_mode"] in ("sequential", "cache_hit")


class TestAgentProcess:
    """Agent.process() 真实调用路径测试"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from session_manager import EnhancedSessionManager
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard

        self.sm = EnhancedSessionManager()
        self.bus = MessageBus()
        self.bb = SharedBlackboard()
        self.mock_llm = _make_mock_llm("这是一款温和的洁面产品，适合敏感肌使用。")

    @pytest.mark.asyncio
    async def test_product_agent_process(self):
        """ProductAgent.process() 真实调用路径"""
        from agents import ProductAgent
        from erp.kingdee_adapter import KingdeeMockAdapter

        agent = ProductAgent()
        agent.set_llm(self.mock_llm)
        agent.set_session_manager(self.sm)
        agent.set_bus(self.bus)
        agent.set_blackboard(self.bb)
        agent.set_erp(KingdeeMockAdapter())

        state = _make_state("玻尿酸精华液的成分是什么？")
        self.sm.create_session(state["session_id"])

        result = await agent.process(state)

        assert result["response"], "Agent 应返回响应"
        assert result["current_agent"] == "产品专家"
        # 验证 LLM 被调用
        self.mock_llm.async_invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_tech_agent_process(self):
        """TechAgent.process() 真实调用路径"""
        from agents import TechAgent

        agent = TechAgent()
        agent.set_llm(self.mock_llm)
        agent.set_session_manager(self.sm)
        agent.set_bus(self.bus)
        agent.set_blackboard(self.bb)

        state = _make_state("敏感肌可以用这款产品吗？")
        self.sm.create_session(state["session_id"])

        result = await agent.process(state)

        assert result["response"]
        assert result["current_agent"] == "技术支持专家"
        self.mock_llm.async_invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_billing_agent_process(self):
        """BillingAgent.process() 真实调用路径"""
        from agents import BillingAgent
        from erp.kingdee_adapter import KingdeeMockAdapter

        agent = BillingAgent()
        agent.set_llm(self.mock_llm)
        agent.set_session_manager(self.sm)
        agent.set_bus(self.bus)
        agent.set_blackboard(self.bb)
        agent.set_erp(KingdeeMockAdapter())

        state = _make_state("我想查询我的订单状态")
        self.sm.create_session(state["session_id"])

        result = await agent.process(state)

        assert result["response"]
        assert result["current_agent"] == "账单专家"
        self.mock_llm.async_invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_complaint_agent_process(self):
        """ComplaintAgent.process() 真实调用路径"""
        from agents import ComplaintAgent

        agent = ComplaintAgent()
        agent.set_llm(self.mock_llm)
        agent.set_session_manager(self.sm)
        agent.set_bus(self.bus)
        agent.set_blackboard(self.bb)

        state = _make_state("我对你们的服务非常不满意！")
        self.sm.create_session(state["session_id"])

        result = await agent.process(state)

        assert result["response"]
        assert result["current_agent"] == "投诉处理专家"
        self.mock_llm.async_invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_general_agent_process(self):
        """GeneralAgent.process() 真实调用路径"""
        from agents import GeneralAgent

        agent = GeneralAgent()
        agent.set_llm(self.mock_llm)
        agent.set_session_manager(self.sm)
        agent.set_bus(self.bus)
        agent.set_blackboard(self.bb)

        state = _make_state("你们有什么产品推荐？")
        self.sm.create_session(state["session_id"])

        result = await agent.process(state)

        assert result["response"]
        assert result["current_agent"] == "通用咨询专家"
        self.mock_llm.async_invoke.assert_called_once()


class TestAgentSessionContext:
    """Agent 会话上下文保持测试"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from session_manager import EnhancedSessionManager
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard
        from agents import ProductAgent
        from erp.kingdee_adapter import KingdeeMockAdapter

        self.sm = EnhancedSessionManager()
        self.bus = MessageBus()
        self.bb = SharedBlackboard()

        self.agent = ProductAgent()
        self.agent.set_session_manager(self.sm)
        self.agent.set_bus(self.bus)
        self.agent.set_blackboard(self.bb)
        self.agent.set_erp(KingdeeMockAdapter())

        self.session_id = "context-test-session"
        self.sm.create_session(self.session_id)

    @pytest.mark.asyncio
    async def test_multi_turn_context_preserved(self):
        """多轮对话上下文保持：第二轮能看到第一轮的历史"""
        responses = ["第一轮回复", "第二轮回复看到了上下文"]
        call_count = 0

        async def mock_invoke(messages, **kwargs):
            nonlocal call_count
            resp = MagicMock()
            # 验证第二轮调用时 messages 中包含第一轮的对话
            if call_count == 1:
                msg_contents = [str(m.content) for m in messages]
                assert any("第一轮" in c or "精华液" in c for c in msg_contents), \
                    "第二轮 LLM 调用应包含第一轮的对话上下文"
            resp.content = responses[call_count]
            call_count += 1
            return resp

        mock_llm = MagicMock()
        mock_llm.async_invoke = mock_invoke
        self.agent.set_llm(mock_llm)

        # 第一轮
        state1 = _make_state("精华液有哪些成分？", self.session_id)
        result1 = await self.agent.process(state1)
        assert result1["response"] == "第一轮回复"

        # 第二轮
        state2 = _make_state("价格是多少？", self.session_id)
        result2 = await self.agent.process(state2)
        assert result2["response"] == "第二轮回复看到了上下文"


class TestDriftDetection:
    """漂移检测集成测试"""

    @pytest.fixture(autouse=True)
    async def setup(self):
        from session_manager import EnhancedSessionManager
        self.sm = EnhancedSessionManager()
        self.session_id = "drift-test"
        await self.sm.create_session(self.session_id)

    @pytest.mark.asyncio
    async def test_repetition_detection(self):
        """重复提问检测"""
        query = "这款面霜适合油性皮肤吗？"
        await self.sm.add_message(self.session_id, query, is_user=True)
        await self.sm.add_message(self.session_id, "适合的，这款面霜质地清爽。", is_user=False)
        await self.sm.add_message(self.session_id, query, is_user=True)

        drift = await self.sm.detect_drift(self.session_id, query)
        # 重复提问阈值为 0.8，相同字符串的相似度为 1.0
        assert drift.get("has_drift") or len(drift.get("drifts", [])) > 0

    @pytest.mark.asyncio
    async def test_topic_drift_detection(self):
        """话题漂移检测：从产品问题突然切换到完全不相关的主题"""
        # 先建立话题上下文（需要至少 2 条用户消息才能比较话题变化）
        await self.sm.add_message(self.session_id, "精华液的成分有哪些？", is_user=True)
        await self.sm.add_message(self.session_id, "包含玻尿酸和烟酰胺。", is_user=False)
        await self.sm.add_message(self.session_id, "这款面霜多少钱？", is_user=True)
        await self.sm.add_message(self.session_id, "128 元。", is_user=False)

        # 切换到完全不同的话题
        drift = await self.sm.detect_drift(self.session_id, "今天天气怎么样？")
        # 话题漂移应该被检测到（Jaccard 相似度很低）
        assert drift.get("drifts"), f"Expected drifts, got: {drift}"
        assert any(d.get("type") in ("topic_drift", "intent_drift", "topic", "intent") for d in drift["drifts"])


class TestCollaborationModes:
    """5 种协作模式独立测试"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from session_manager import EnhancedSessionManager
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard
        from collaboration.orchestrator import CollaborationOrchestrator
        from erp.kingdee_adapter import KingdeeMockAdapter
        from agents import (
            ProductAgent, TechAgent, BillingAgent,
            ComplaintAgent, GeneralAgent,
        )

        self.sm = EnhancedSessionManager()
        self.bus = MessageBus()
        self.bb = SharedBlackboard()
        self.erp = KingdeeMockAdapter()
        self.mock_llm = _make_mock_llm("根据您的需求，为您推荐以下方案。")

        self.orchestrator = CollaborationOrchestrator(self.bus, self.bb)

        # 构建 agents_dict
        self.agents_dict = {}
        for name, cls in [
            ("product_agent", ProductAgent),
            ("tech_agent", TechAgent),
            ("billing_agent", BillingAgent),
            ("complaint_agent", ComplaintAgent),
            ("general_agent", GeneralAgent),
        ]:
            agent = cls()
            agent.set_llm(self.mock_llm)
            agent.set_session_manager(self.sm)
            agent.set_bus(self.bus)
            agent.set_blackboard(self.bb)
            agent.set_erp(self.erp)
            self.agents_dict[name] = agent

    @pytest.mark.asyncio
    async def test_sequential_mode(self):
        """Sequential 模式：单 Agent 顺序处理"""
        from collaboration.modes import SequentialMode

        mode = SequentialMode(bus=self.bus, bb=self.bb)
        state = _make_state("精华液多少钱？")
        self.sm.create_session(state["session_id"])

        result = await mode.execute(
            self.agents_dict, state,
            {"primary_agent": "product_agent"}
        )

        assert result["response"], "Sequential 模式应返回响应"
        assert result["mode"] == "sequential"
        assert "product_agent" in result["agents_used"]

    @pytest.mark.asyncio
    async def test_parallel_mode(self):
        """Parallel 模式：多 Agent 并发"""
        from collaboration.modes import ParallelMode

        mode = ParallelMode(bus=self.bus, bb=self.bb)
        state = _make_state("精华液成分和物流状态？")
        self.sm.create_session(state["session_id"])

        result = await mode.execute(
            self.agents_dict, state,
            {"agent_list": ["product_agent", "billing_agent"]}
        )

        assert result["response"]
        assert result["mode"] == "parallel"
        assert len(result["agents_used"]) >= 1

    @pytest.mark.asyncio
    async def test_consultation_mode(self):
        """Consultation 模式：主 Agent + 顾问"""
        from collaboration.modes import ConsultationMode

        mode = ConsultationMode(bus=self.bus, bb=self.bb)
        state = _make_state("精华液适合敏感肌吗？成分安全吗？")
        self.sm.create_session(state["session_id"])

        result = await mode.execute(
            self.agents_dict, state,
            {"primary_agent": "product_agent", "consult_agents": ["tech_agent"]}
        )

        assert result["response"]
        assert result["mode"] == "consultation"

    @pytest.mark.asyncio
    async def test_hierarchical_mode(self):
        """Hierarchical 模式：协调者分配子任务"""
        from collaboration.modes import HierarchicalMode

        mode = HierarchicalMode(bus=self.bus, bb=self.bb)
        state = _make_state("我要投诉产品导致过敏！")
        self.sm.create_session(state["session_id"])

        result = await mode.execute(
            self.agents_dict, state,
            {"coordinator": "general_agent", "sub_tasks": {"complaint_agent": "投诉处理"}}
        )

        assert result["response"]
        assert result["mode"] == "hierarchical"

    @pytest.mark.asyncio
    async def test_react_mode(self):
        """ReAct 模式：推理 + 工具调用"""
        from collaboration.modes import ReActMode

        mode = ReActMode(bus=self.bus, bb=self.bb)
        state = _make_state("帮我查一下订单 1001 的物流状态，并推荐相关产品")
        self.sm.create_session(state["session_id"])

        result = await mode.execute(
            self.agents_dict, state,
            {"primary_agent": "react_agent"}
        )

        assert result["response"]
        assert result["mode"] == "react"


class TestErrorHandling:
    """错误降级处理测试"""

    @pytest.fixture(autouse=True)
    def setup(self):
        import multi_agent_customer_service as graph_mod
        self.graph_mod = graph_mod
        graph_mod.llm = None
        graph_mod.agents_dict = {}
        graph_mod.router = None
        graph_mod.response_agent = None
        graph_mod.knowledge_base = None
        graph_mod.tool_registry = None

    def teardown_method(self):
        self.graph_mod.llm = None
        self.graph_mod.agents_dict = {}

    @pytest.mark.asyncio
    async def test_llm_timeout_graceful_degradation(self):
        """LLM 超时 → 降级到 fallback 响应"""
        from agents import ProductAgent
        from session_manager import EnhancedSessionManager
        from erp.kingdee_adapter import KingdeeMockAdapter

        sm = EnhancedSessionManager()
        agent = ProductAgent()
        agent.set_session_manager(sm)
        agent.set_erp(KingdeeMockAdapter())

        # Mock LLM 超时
        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=TimeoutError("LLM 请求超时"))
        agent.set_llm(mock_llm)

        state = _make_state("产品信息")
        sm.create_session(state["session_id"])

        result = await agent.process(state)

        # 超时应降级到 fallback 响应
        assert result["response"]
        assert "错误" in result["response"] or "重试" in result["response"] or "抱歉" in result["response"]

    @pytest.mark.asyncio
    async def test_llm_connection_error_graceful_degradation(self):
        """LLM 连接错误 → 降级到 fallback"""
        from agents import TechAgent
        from session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        agent = TechAgent()
        agent.set_session_manager(sm)

        mock_llm = MagicMock()
        mock_llm.async_invoke = AsyncMock(side_effect=ConnectionError("连接被拒绝"))
        agent.set_llm(mock_llm)

        state = _make_state("使用方法")
        sm.create_session(state["session_id"])

        result = await agent.process(state)

        assert result["response"]
        assert "错误" in result["response"] or "重试" in result["response"] or "抱歉" in result["response"]

    @pytest.mark.asyncio
    async def test_erp_unavailable_graceful_degradation(self):
        """ERP 不可用 → Agent 仍能返回响应"""
        from agents import BillingAgent
        from session_manager import EnhancedSessionManager

        sm = EnhancedSessionManager()
        mock_llm = _make_mock_llm("订单信息暂时无法获取，请稍后重试。")

        agent = BillingAgent()
        agent.set_llm(mock_llm)
        agent.set_session_manager(sm)

        # ERP 模拟异常
        mock_erp = MagicMock()
        mock_erp.query_order = AsyncMock(side_effect=Exception("ERP 连接失败"))
        mock_erp.query_inventory = AsyncMock(side_effect=Exception("ERP 连接失败"))
        agent.set_erp(mock_erp)

        state = _make_state("我的订单在哪里？")
        sm.create_session(state["session_id"])

        result = await agent.process(state)

        # ERP 失败不影响最终响应
        assert result["response"]


class TestRoutingLogic:
    """路由逻辑测试（Mock LLM 路由器）"""

    @pytest.mark.asyncio
    async def test_fast_path_routes_to_sequential(self):
        """低复杂度查询 → fast_path=True → Sequential"""
        from router.query_router import QueryRouter, RoutingResult
        from collaboration.orchestrator import CollaborationOrchestrator
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard

        bus = MessageBus()
        bb = SharedBlackboard()
        orch = CollaborationOrchestrator(bus, bb)

        # 模拟低复杂度路由结果
        routing = RoutingResult(
            query_type="general_inquiry",
            agent_name="general_agent",
            complexity=20,
            fast_path=True,
        )

        mode_name = orch.select_mode_name(routing, _make_state("你好"))
        assert mode_name == "sequential"

    @pytest.mark.asyncio
    async def test_complaint_routes_to_hierarchical(self):
        """投诉查询 → Hierarchical"""
        from router.query_router import RoutingResult
        from collaboration.orchestrator import CollaborationOrchestrator
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard

        bus = MessageBus()
        bb = SharedBlackboard()
        orch = CollaborationOrchestrator(bus, bb)

        routing = RoutingResult(
            query_type="complaint",
            agent_name="complaint_agent",
            complexity=70,
            fast_path=False,
        )

        mode_name = orch.select_mode_name(
            routing, _make_state("我要投诉！皮肤过敏了")
        )
        assert mode_name == "hierarchical"

    @pytest.mark.asyncio
    async def test_high_complexity_multi_domain_routes_to_react(self):
        """高复杂度 + 多领域 → ReAct"""
        from router.query_router import RoutingResult
        from collaboration.orchestrator import CollaborationOrchestrator
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard

        bus = MessageBus()
        bb = SharedBlackboard()
        orch = CollaborationOrchestrator(bus, bb)

        routing = RoutingResult(
            query_type="multi_domain",
            agent_name="product_agent",
            complexity=80,
            fast_path=False,
        )

        # 包含产品关键词 + 订单关键词 → 多领域
        query = "这款精华液成分安全吗？另外帮我查一下订单物流"
        mode_name = orch.select_mode_name(routing, _make_state(query))
        assert mode_name == "react"
