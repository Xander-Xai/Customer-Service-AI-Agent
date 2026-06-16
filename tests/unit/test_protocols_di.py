"""
Protocol 类型 + 依赖注入 测试（v5.1）
"""

import pytest


class TestProtocols:
    """Protocol 接口定义测试"""

    def test_llm_protocol_is_runtime_checkable(self):
        """LLMProtocol 支持运行时类型检查"""
        from core.protocols import LLMProtocol

        assert hasattr(LLMProtocol, "async_invoke")
        assert hasattr(LLMProtocol, "async_invoke_stream")

    def test_erp_protocol_is_runtime_checkable(self):
        """ERPProtocol 支持运行时类型检查"""
        from core.protocols import ERPProtocol

        assert hasattr(ERPProtocol, "query_product")
        assert hasattr(ERPProtocol, "query_order")

    def test_knowledge_base_protocol(self):
        """KnowledgeBaseProtocol 定义了必要方法"""
        from core.protocols import KnowledgeBaseProtocol

        assert hasattr(KnowledgeBaseProtocol, "query")
        assert hasattr(KnowledgeBaseProtocol, "query_multiple")

    def test_tool_registry_protocol(self):
        """ToolRegistryProtocol 定义了必要方法"""
        from core.protocols import ToolRegistryProtocol

        assert hasattr(ToolRegistryProtocol, "list_tools")
        assert hasattr(ToolRegistryProtocol, "execute")

    def test_session_manager_protocol(self):
        """SessionManagerProtocol 定义了必要方法"""
        from core.protocols import SessionManagerProtocol

        assert hasattr(SessionManagerProtocol, "get_session")
        assert hasattr(SessionManagerProtocol, "add_message")

    def test_openai_client_satisfies_llm_protocol(self):
        """OpenAICompatibleClient 满足 LLMProtocol"""
        from core.protocols import LLMProtocol
        from llm.client import OpenAICompatibleClient

        assert issubclass(OpenAICompatibleClient, LLMProtocol)

    def test_rule_based_llm_has_async_invoke(self):
        """RuleBasedLLM 实现了 async_invoke（兼容 LLM 核心接口）"""
        from llm.rule_based_llm import RuleBasedLLM

        assert hasattr(RuleBasedLLM, "async_invoke")


class TestAgentConstructorInjection:
    """Agent 构造函数注入测试"""

    def test_product_agent_accepts_llm_in_constructor(self):
        """ProductAgent 支持构造函数注入 llm"""
        from agents.product_agent import ProductAgent

        class MockLLM:
            async def async_invoke(self, messages, **kw):
                return None

            async def async_invoke_stream(self, messages, **kw):
                yield ""

        agent = ProductAgent(llm=MockLLM())
        assert agent.llm is not None

    async def test_agent_without_llm_raises_on_process(self):
        """未注入 LLM 的 Agent 在处理时抛出 RuntimeError"""
        from agents.product_agent import ProductAgent

        agent = ProductAgent()  # 不传 llm
        assert agent.llm is None

        state = {"customer_query": "测试", "session_id": "test"}
        with pytest.raises(RuntimeError, match="LLM 未注入"):
            await agent.process(state)

    def test_all_agents_accept_llm_constructor(self):
        """所有 Agent 子类都支持 llm 构造参数"""
        from agents import (
            BillingAgent,
            ComplaintAgent,
            GeneralAgent,
            ProductAgent,
            ReActAgent,
            TechAgent,
        )

        for cls in [ProductAgent, TechAgent, BillingAgent, ComplaintAgent, GeneralAgent]:
            agent = cls(llm=None)
            assert agent.llm is None

        react = ReActAgent(llm=None)
        assert react.llm is None

    def test_backward_compatible_no_llm(self):
        """不传 llm 参数时向后兼容"""
        from agents.product_agent import ProductAgent

        agent = ProductAgent()  # 旧写法
        assert agent.llm is None
        assert agent.name == "产品专家"


class TestDependencyInjection:
    """FastAPI 依赖注入辅助测试"""

    def test_get_container_from_app_state(self):
        """get_container 从 app.state 获取容器"""
        from unittest.mock import MagicMock

        from fastapi import Request

        mock_container = MagicMock()
        mock_request = MagicMock(spec=Request)
        mock_request.app.state.container = mock_container

        from api.dependencies import get_container

        result = get_container(mock_request)
        assert result is mock_container

    def test_get_container_raises_when_missing(self):
        """app.state 无 container 时抛出 RuntimeError"""
        from unittest.mock import MagicMock

        from fastapi import Request

        mock_request = MagicMock(spec=Request)
        mock_request.app.state.container = None

        from api.dependencies import get_container

        with pytest.raises(RuntimeError, match="ServiceContainer 未初始化"):
            get_container(mock_request)
