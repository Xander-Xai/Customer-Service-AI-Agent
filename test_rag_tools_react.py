"""
v3.5 RAG + Function Calling + ReAct 专项测试
覆盖：工具注册、ERP 工具、知识库、LLM 工具调用、ReAct 循环、图集成
无需 LLM API 和网络，纯逻辑测试（ChromaDB 使用内存模式）

运行: pytest test_rag_tools_react.py -v
"""
import os
import sys
import json
import asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


# ===== 1. 工具注册测试 =====

class TestToolRegistry:
    """工具注册框架单元测试"""

    def test_register_and_list(self):
        """注册工具后可在列表中找到"""
        from tools.tool_registry import ToolRegistry
        registry = ToolRegistry()
        registry.register(
            name="test_tool",
            description="测试工具",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            handler=lambda args: "ok",
        )
        assert "test_tool" in registry.list_tools()

    def test_get_openai_tools_format(self):
        """返回的 OpenAI tools 格式正确"""
        from tools.tool_registry import ToolRegistry
        registry = ToolRegistry()
        registry.register(
            name="search",
            description="搜索",
            parameters={"type": "object", "properties": {"keyword": {"type": "string"}}},
            handler=lambda args: "result",
        )
        tools = registry.get_openai_tools()
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "search"
        assert tools[0]["function"]["description"] == "搜索"
        assert "keyword" in tools[0]["function"]["parameters"]["properties"]

    @pytest.mark.asyncio
    async def test_execute_existing_tool(self):
        """执行已注册工具返回正确结果"""
        from tools.tool_registry import ToolRegistry
        registry = ToolRegistry()

        async def handler(args):
            return f"Hello {args.get('name', 'World')}"

        registry.register(
            name="greet",
            description="打招呼",
            parameters={"type": "object", "properties": {"name": {"type": "string"}}},
            handler=handler,
        )
        result = await registry.execute("greet", {"name": "测试"})
        assert result == "Hello 测试"

    @pytest.mark.asyncio
    async def test_execute_missing_tool(self):
        """执行不存在的工具返回错误提示"""
        from tools.tool_registry import ToolRegistry
        registry = ToolRegistry()
        result = await registry.execute("nonexistent", {})
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_execute_tool_with_error(self):
        """工具执行异常时返回错误信息"""
        from tools.tool_registry import ToolRegistry
        registry = ToolRegistry()

        async def bad_handler(args):
            raise ValueError("boom")

        registry.register(name="bad", description="坏工具", parameters={}, handler=bad_handler)
        result = await registry.execute("bad", {})
        assert "执行失败" in result and "稍后重试" in result

    def test_multiple_tools(self):
        """注册多个工具"""
        from tools.tool_registry import ToolRegistry
        registry = ToolRegistry()
        for i in range(5):
            registry.register(
                name=f"tool_{i}", description=f"工具{i}",
                parameters={}, handler=lambda args: "ok",
            )
        assert len(registry.list_tools()) == 5
        assert len(registry.get_openai_tools()) == 5


# ===== 2. ERP 工具测试 =====

class TestERPTools:
    """ERP 工具包装测试"""

    @pytest.mark.asyncio
    async def test_create_erp_tools(self):
        """ERP 工具注册成功"""
        from tools.erp_tools import create_erp_tools
        from erp.kingdee_adapter import KingdeeMockAdapter
        erp = KingdeeMockAdapter()
        registry = create_erp_tools(erp)
        assert "query_product" in registry.list_tools()
        assert "query_inventory" in registry.list_tools()
        assert "query_order" in registry.list_tools()
        assert "query_customer" in registry.list_tools()

    @pytest.mark.asyncio
    async def test_query_product_tool(self):
        """产品查询工具返回正确格式"""
        from tools.erp_tools import create_erp_tools
        from erp.kingdee_adapter import KingdeeMockAdapter
        registry = create_erp_tools(KingdeeMockAdapter())
        result = await registry.execute("query_product", {"keyword": "玫瑰"})
        assert "玫瑰焕颜精华液" in result
        assert "298" in result

    @pytest.mark.asyncio
    async def test_query_inventory_tool(self):
        """库存查询工具正常工作"""
        from tools.erp_tools import create_erp_tools
        from erp.kingdee_adapter import KingdeeMockAdapter
        registry = create_erp_tools(KingdeeMockAdapter())
        result = await registry.execute("query_inventory", {"keyword": "精华"})
        assert "玫瑰焕颜精华液" in result
        assert "1200" in result

    @pytest.mark.asyncio
    async def test_query_order_tool(self):
        """订单查询工具正常工作"""
        from tools.erp_tools import create_erp_tools
        from erp.kingdee_adapter import KingdeeMockAdapter
        registry = create_erp_tools(KingdeeMockAdapter())
        result = await registry.execute("query_order", {"order_id": "ORD20260530001"})
        assert "ORD20260530001" in result
        assert "已发货" in result

    @pytest.mark.asyncio
    async def test_query_customer_tool(self):
        """客户查询工具正常工作"""
        from tools.erp_tools import create_erp_tools
        from erp.kingdee_adapter import KingdeeMockAdapter
        registry = create_erp_tools(KingdeeMockAdapter())
        result = await registry.execute("query_customer", {"customer_id": "C001"})
        assert "王女士" in result
        assert "VIP" in result

    @pytest.mark.asyncio
    async def test_query_product_not_found(self):
        """查询不存在的产品返回提示"""
        from tools.erp_tools import create_erp_tools
        from erp.kingdee_adapter import KingdeeMockAdapter
        registry = create_erp_tools(KingdeeMockAdapter())
        result = await registry.execute("query_product", {"keyword": "不存在的产品XYZ"})
        assert "未找到" in result


# ===== 3. RAG 知识库测试 =====

class TestKnowledgeBase:
    """ChromaDB 知识库测试（内存模式）"""

    def test_init_available(self):
        """知识库初始化成功"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        kb = CosmeticsKnowledgeBase()
        assert kb.available

    def test_add_and_query(self):
        """添加文档后可查询到"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        kb = CosmeticsKnowledgeBase()
        kb.add_documents(
            "test_col",
            ["玻尿酸是最常见的保湿成分", "烟酰胺具有美白功效", "视黄醇是抗衰老金标准"],
            [{"topic": "保湿"}, {"topic": "美白"}, {"topic": "抗老"}],
        )
        assert kb.get_collection_count("test_col") == 3

    def test_seed_if_empty(self):
        """seed_if_empty 仅在空 collection 时执行"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        kb = CosmeticsKnowledgeBase()

        call_count = [0]
        def seed_fn(kb, collection_name="test_seed"):
            call_count[0] += 1
            kb.add_documents(collection_name, ["test doc"], [{}], ["id_0"])

        kb.seed_if_empty("test_seed", seed_fn)
        assert call_count[0] == 1
        assert kb.get_collection_count("test_seed") == 1

        # 第二次不应执行
        kb.seed_if_empty("test_seed", seed_fn)
        assert call_count[0] == 1  # 未增加

    @pytest.mark.asyncio
    async def test_query_async(self):
        """异步查询返回结果"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        kb = CosmeticsKnowledgeBase()
        kb.add_documents("async_col", ["保湿产品推荐", "美白产品推荐", "防晒产品推荐"])
        results = await kb.query("async_col", "保湿", n_results=2)
        assert len(results) > 0
        assert "content" in results[0]
        assert "distance" in results[0]

    @pytest.mark.asyncio
    async def test_query_multiple_collections(self):
        """跨多个 collection 查询"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        kb = CosmeticsKnowledgeBase()
        kb.add_documents("col_a", ["产品成分知识A"])
        kb.add_documents("col_b", ["技术支持知识B"])
        results = await kb.query_multiple(["col_a", "col_b"], "产品", n_results=3)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_query_empty_collection(self):
        """查询空 collection 返回空列表"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        kb = CosmeticsKnowledgeBase()
        results = await kb.query("empty_col", "test")
        assert results == []

    def test_metadata_preserved(self):
        """文档 metadata 正确保留"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        kb = CosmeticsKnowledgeBase()
        kb.add_documents(
            "meta_col",
            ["测试文档"],
            [{"category": "test", "topic": "验证"}],
            ["meta_0"],
        )
        # 直接从 collection 获取验证
        col = kb.get_or_create_collection("meta_col")
        result = col.get(ids=["meta_0"])
        assert result["metadatas"][0]["category"] == "test"


# ===== 4. 种子数据测试 =====

class TestSeedData:
    """种子数据加载测试"""

    def test_seed_product_knowledge(self):
        """产品知识种子数据加载成功"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        from rag.seed_data import seed_product_knowledge
        kb = CosmeticsKnowledgeBase()
        seed_product_knowledge(kb)
        assert kb.get_collection_count("product_knowledge") >= 20

    def test_seed_faq(self):
        """FAQ 种子数据加载成功"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        from rag.seed_data import seed_faq
        kb = CosmeticsKnowledgeBase()
        seed_faq(kb)
        assert kb.get_collection_count("faq") >= 15

    def test_seed_tech_support(self):
        """技术支持种子数据加载成功"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        from rag.seed_data import seed_tech_support
        kb = CosmeticsKnowledgeBase()
        seed_tech_support(kb)
        assert kb.get_collection_count("tech_support") >= 10

    @pytest.mark.asyncio
    async def test_seed_data_retrieval(self):
        """种子数据可被语义检索到"""
        from rag.knowledge_base import CosmeticsKnowledgeBase
        from rag.seed_data import seed_product_knowledge, seed_faq
        kb = CosmeticsKnowledgeBase()
        seed_product_knowledge(kb)
        seed_faq(kb)

        # 产品成分查询
        results = await kb.query("product_knowledge", "玻尿酸保湿效果", n_results=3)
        assert len(results) > 0
        assert any("玻尿酸" in r["content"] for r in results)

        # FAQ 查询
        results = await kb.query("faq", "发货要几天", n_results=3)
        assert len(results) > 0
        assert any("发货" in r["content"] for r in results)


# ===== 5. LLM 客户端 Function Calling 测试 =====

class TestLLMToolCalling:
    """OpenAICompatibleClient 工具调用支持测试"""

    def test_custom_response_with_tool_calls(self):
        """CustomResponse 支持 tool_calls 属性"""
        from core.monitoring import CustomResponse
        resp = CustomResponse("hello")
        assert resp.content == "hello"
        assert resp.tool_calls is None

        resp2 = CustomResponse("call", [{"id": "c1", "name": "search", "arguments": "{}"}])
        assert resp2.tool_calls is not None
        assert len(resp2.tool_calls) == 1
        assert resp2.tool_calls[0]["name"] == "search"

    def test_custom_response_backward_compat(self):
        """CustomResponse 向后兼容：仅传 content 参数"""
        from core.monitoring import CustomResponse
        resp = CustomResponse(content="test")
        assert resp.content == "test"
        assert resp.tool_calls is None


# ===== 6. BaseAgent RAG + 工具方法测试 =====

class TestBaseAgentCapabilities:
    """BaseAgent 新增能力测试"""

    @pytest.mark.asyncio
    async def test_retrieve_knowledge_no_kb(self):
        """无知识库时 _retrieve_knowledge 返回空字符串"""
        from agents.product_agent import ProductAgent
        agent = ProductAgent()
        result = await agent._retrieve_knowledge("test query")
        assert result == ""

    @pytest.mark.asyncio
    async def test_retrieve_knowledge_with_kb(self):
        """有知识库时 _retrieve_knowledge 返回检索结果"""
        from agents.product_agent import ProductAgent
        from rag.knowledge_base import CosmeticsKnowledgeBase
        from rag.seed_data import seed_product_knowledge

        kb = CosmeticsKnowledgeBase()
        seed_product_knowledge(kb)

        agent = ProductAgent()
        agent.set_knowledge_base(kb)
        result = await agent._retrieve_knowledge("玻尿酸", collections=["product_knowledge"])
        assert "知识库" in result
        assert len(result) > 0

    def test_set_knowledge_base(self):
        """set_knowledge_base 注入知识库"""
        from agents.product_agent import ProductAgent
        agent = ProductAgent()
        assert agent.knowledge_base is None
        agent.set_knowledge_base("mock_kb")
        assert agent.knowledge_base == "mock_kb"

    def test_set_tool_registry(self):
        """set_tool_registry 注入工具注册"""
        from agents.product_agent import ProductAgent
        agent = ProductAgent()
        assert agent.tool_registry is None
        agent.set_tool_registry("mock_registry")
        assert agent.tool_registry == "mock_registry"


# ===== 7. ReAct Agent 测试 =====

class TestReActAgent:
    """ReAct Agent 单元测试"""

    def test_init(self):
        """ReActAgent 初始化属性正确"""
        from agents.react_agent import ReActAgent
        agent = ReActAgent()
        assert agent.name == "ReAct推理专家"
        assert agent.max_iterations == 5

    def test_custom_max_iterations(self):
        """自定义 max_iterations"""
        from agents.react_agent import ReActAgent
        agent = ReActAgent(max_iterations=3)
        assert agent.max_iterations == 3

    def test_inherits_base_agent(self):
        """ReActAgent 继承 BaseAgent"""
        from agents.react_agent import ReActAgent
        from agents.base_agent import BaseAgent
        agent = ReActAgent()
        assert isinstance(agent, BaseAgent)
        assert hasattr(agent, '_retrieve_knowledge')
        assert hasattr(agent, '_process_with_tools')


# ===== 8. 图集成测试 =====

class TestGraphIntegration:
    """LangGraph 图集成测试"""

    def test_graph_has_react_node(self):
        """构建的图包含 react 节点"""
        from multi_agent_customer_service import make_graph
        app = make_graph()
        # 编译后的图应有 react 节点（通过 nodes 检查）
        nodes = list(app.get_graph().nodes)
        assert "react" in nodes

    def test_graph_has_all_modes(self):
        """图包含所有 5 种协作模式节点"""
        from multi_agent_customer_service import make_graph
        app = make_graph()
        nodes = list(app.get_graph().nodes)
        for mode in ["sequential", "parallel", "consultation", "hierarchical", "react"]:
            assert mode in nodes, f"缺少节点: {mode}"

    def _run_async(self, coro):
        """在新事件循环中运行异步协程"""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_agents_dict_includes_react(self):
        """agents_dict 包含 react_agent"""
        from multi_agent_customer_service import initialize_agents
        agents = self._run_async(initialize_agents())
        assert "react_agent" in agents

    def test_product_agent_has_knowledge_base(self):
        """ProductAgent 注入了知识库"""
        from multi_agent_customer_service import initialize_agents
        agents = self._run_async(initialize_agents())
        agent = agents.get("product_agent")
        assert agent is not None
        assert agent.knowledge_base is not None

    def test_tech_agent_has_knowledge_base(self):
        """TechAgent 注入了知识库"""
        from multi_agent_customer_service import initialize_agents
        agents = self._run_async(initialize_agents())
        agent = agents.get("tech_agent")
        assert agent is not None
        assert agent.knowledge_base is not None

    def test_react_agent_has_tool_registry(self):
        """ReActAgent 注入了工具注册"""
        from multi_agent_customer_service import initialize_agents
        agents = self._run_async(initialize_agents())
        agent = agents.get("react_agent")
        assert agent is not None
        assert agent.tool_registry is not None
        assert len(agent.tool_registry.list_tools()) == 4

    def test_version_updated(self):
        """版本号已更新为 3.8.0"""
        from config import VERSION
        assert VERSION == "3.8.0"


# ===== 9. 协作模式测试 =====

class TestReActMode:
    """ReAct 协作模式测试"""

    @pytest.mark.asyncio
    async def test_react_mode_registered(self):
        """orchestrator 注册了 react 模式"""
        from collaboration.orchestrator import CollaborationOrchestrator
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard
        orch = CollaborationOrchestrator(MessageBus(), SharedBlackboard())
        assert "react" in orch._modes

    @pytest.mark.asyncio
    async def test_react_mode_fallback(self):
        """react_agent 不存在时回退到 primary agent"""
        from collaboration.modes import ReActMode
        mode = ReActMode()

        class MockAgent:
            async def process_with_retry(self, state):
                return {"response": "fallback response"}

        agents = {"general_agent": MockAgent()}
        state = {"customer_query": "test"}
        context = {"primary_agent": "general_agent"}
        result = await mode.execute(agents, state, context)
        assert result["mode"] == "react"
        assert result["response"] == "fallback response"


# ===== 10. 产品/技术 Agent RAG 集成测试 =====

class TestAgentRAGIntegration:
    """Agent RAG 集成测试"""

    @pytest.mark.asyncio
    async def test_product_agent_with_rag(self):
        """ProductAgent 集成 RAG 后 extra_context 包含知识库内容"""
        from agents.product_agent import ProductAgent
        from rag.knowledge_base import CosmeticsKnowledgeBase
        from rag.seed_data import seed_product_knowledge

        kb = CosmeticsKnowledgeBase()
        seed_product_knowledge(kb)

        agent = ProductAgent()
        agent.set_knowledge_base(kb)

        # 测试 _retrieve_knowledge 直接调用
        result = await agent._retrieve_knowledge(
            "烟酰胺的功效", collections=["product_knowledge"]
        )
        assert "知识库" in result
        assert len(result) > 20  # 应该有实质内容

    @pytest.mark.asyncio
    async def test_tech_agent_with_rag(self):
        """TechAgent 集成 RAG 后可检索技术支持文档"""
        from agents.tech_agent import TechAgent
        from rag.knowledge_base import CosmeticsKnowledgeBase
        from rag.seed_data import seed_tech_support

        kb = CosmeticsKnowledgeBase()
        seed_tech_support(kb)

        agent = TechAgent()
        agent.set_knowledge_base(kb)

        result = await agent._retrieve_knowledge(
            "过敏了怎么办", collections=["tech_support"]
        )
        assert "知识库" in result
        assert "过敏" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
