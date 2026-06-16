"""
测试 agents/base_agent A/B 变体 + 漂移修复 + billing_agent
补齐覆盖率至 80%+ 门槛
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── BaseAgent._resolve_prompt_for_variant ─────────────────────────


class TestResolvePromptVariant:
    def _make_agent(self):
        from agents.base_agent import BaseAgent

        class _TestAgent(BaseAgent):
            async def process(self, state):
                return state

        agent = _TestAgent(name="test_agent", role="测试", expertise=["测试"])
        agent.llm = AsyncMock()
        agent.session_manager = AsyncMock()
        agent.bus = AsyncMock()
        agent.bb = AsyncMock()
        return agent

    @patch("agents.base_agent.AB_TEST_ENABLED", False)
    def test_ab_disabled_returns_original(self):
        agent = self._make_agent()
        prompt, variant, exp = agent._resolve_prompt_for_variant("原始prompt", "user1")
        assert prompt == "原始prompt"
        assert variant == "control"
        assert exp is None

    @patch("agents.base_agent.AB_TEST_ENABLED", True)
    def test_no_ab_manager_returns_original(self):
        agent = self._make_agent()
        agent.ab_test_manager = None
        prompt, variant, exp = agent._resolve_prompt_for_variant("原始prompt", "user1")
        assert prompt == "原始prompt"

    @patch("agents.base_agent.AB_TEST_ENABLED", True)
    def test_no_user_id_returns_original(self):
        agent = self._make_agent()
        agent.ab_test_manager = MagicMock()
        prompt, variant, exp = agent._resolve_prompt_for_variant("原始prompt", "")
        assert prompt == "原始prompt"

    @patch("agents.base_agent.AB_TEST_ENABLED", True)
    def test_experiment_not_found(self):
        agent = self._make_agent()
        mgr = MagicMock()
        mgr.experiments = {}
        agent.ab_test_manager = mgr

        prompt, variant, exp = agent._resolve_prompt_for_variant("原始", "user1")
        assert prompt == "原始"
        assert variant == "control"

    @patch("agents.base_agent.AB_TEST_ENABLED", True)
    def test_experiment_inactive(self):
        agent = self._make_agent()
        mgr = MagicMock()
        mgr.experiments = {"prompt_test_agent": {"active": False}}
        agent.ab_test_manager = mgr

        prompt, variant, exp = agent._resolve_prompt_for_variant("原始", "user1")
        assert prompt == "原始"

    @patch("agents.base_agent.AB_TEST_ENABLED", True)
    def test_variant_has_custom_prompt(self):
        agent = self._make_agent()
        agent.prompt_variants = {"variant_b": "自定义prompt B"}

        mgr = MagicMock()
        mgr.experiments = {"prompt_test_agent": {"active": True}}
        mgr.assign_variant.return_value = "variant_b"
        agent.ab_test_manager = mgr

        prompt, variant, exp = agent._resolve_prompt_for_variant("原始", "user1")
        assert prompt == "自定义prompt B"
        assert variant == "variant_b"

    @patch("agents.base_agent.AB_TEST_ENABLED", True)
    def test_variant_no_custom_prompt_returns_original(self):
        agent = self._make_agent()
        agent.prompt_variants = {}

        mgr = MagicMock()
        mgr.experiments = {"prompt_test_agent": {"active": True}}
        mgr.assign_variant.return_value = "variant_c"
        agent.ab_test_manager = mgr

        prompt, variant, exp = agent._resolve_prompt_for_variant("原始", "user1")
        assert prompt == "原始"
        assert variant == "variant_c"

    @patch("agents.base_agent.AB_TEST_ENABLED", True)
    def test_exception_returns_original(self):
        agent = self._make_agent()
        mgr = MagicMock()
        mgr.experiments = {"prompt_test_agent": {"active": True}}
        mgr.assign_variant.side_effect = RuntimeError("boom")
        agent.ab_test_manager = mgr

        prompt, variant, exp = agent._resolve_prompt_for_variant("原始", "user1")
        assert prompt == "原始"
        assert variant == "control"

    @patch("agents.base_agent.AB_TEST_ENABLED", True)
    def test_custom_experiment_name(self):
        agent = self._make_agent()
        mgr = MagicMock()
        mgr.experiments = {"custom_exp": {"active": True}}
        mgr.assign_variant.return_value = "control"
        agent.ab_test_manager = mgr

        prompt, variant, exp = agent._resolve_prompt_for_variant(
            "原始", "user1", experiment_name="custom_exp"
        )
        assert exp == "custom_exp"


# ── BaseAgent._handle_drift ──────────────────────────────────────


class TestHandleDrift:
    def _make_agent(self):
        from agents.base_agent import BaseAgent

        class _TestAgent(BaseAgent):
            async def process(self, state):
                return state

        agent = _TestAgent(name="test_agent", role="测试", expertise=["测试"])
        agent.logger = MagicMock()
        return agent

    def test_no_drift_returns_empty(self):
        agent = self._make_agent()
        result = agent._handle_drift("query", {"has_drift": False})
        assert result == ""

    def test_drift_with_escalation(self):
        agent = self._make_agent()
        result = agent._handle_drift(
            "query",
            {
                "has_drift": True,
                "escalation": {"escalate": True, "reason": "频繁漂移"},
                "drifts": [],
            },
        )
        assert "漂移升级提示" in result

    def test_drift_with_repair_strategy(self):
        agent = self._make_agent()
        result = agent._handle_drift(
            "query",
            {
                "has_drift": True,
                "escalation": None,
                "drifts": [{"type": "topic_shift"}],
            },
        )
        assert isinstance(result, str)

    def test_drift_with_no_matching_strategy(self):
        agent = self._make_agent()
        result = agent._handle_drift(
            "query",
            {
                "has_drift": True,
                "escalation": None,
                "drifts": [{"type": "unknown_drift_type"}],
            },
        )
        assert isinstance(result, str)

    def test_drift_escalation_no_reason(self):
        agent = self._make_agent()
        result = agent._handle_drift(
            "query",
            {
                "has_drift": True,
                "escalation": {"escalate": True},
                "drifts": [],
            },
        )
        assert "漂移升级提示" in result

    def test_drift_no_repairs_returns_empty(self):
        agent = self._make_agent()
        result = agent._handle_drift(
            "query",
            {
                "has_drift": True,
                "escalation": None,
                "drifts": [],
            },
        )
        assert result == ""


# ── BaseAgent._get_conversation_context ───────────────────────────


class TestGetConversationContext:
    def _make_agent(self):
        from agents.base_agent import BaseAgent

        class _TestAgent(BaseAgent):
            async def process(self, state):
                return state

        agent = _TestAgent(name="test_agent", role="测试", expertise=["测试"])
        agent.session_manager = AsyncMock()
        agent.logger = MagicMock()
        return agent

    @pytest.mark.asyncio
    async def test_returns_context_text(self):
        agent = self._make_agent()
        agent.session_manager.get_conversation_context = AsyncMock(
            return_value=[
                {"is_user": True, "content": "你好"},
                {"is_user": False, "content": "你好！"},
            ]
        )
        result = await agent._get_conversation_context("sid1")
        assert "用户: 你好" in result
        assert "AI: 你好！" in result

    @pytest.mark.asyncio
    async def test_empty_context_returns_empty(self):
        agent = self._make_agent()
        agent.session_manager.get_conversation_context = AsyncMock(return_value=[])
        result = await agent._get_conversation_context("sid1")
        assert result == ""

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        agent = self._make_agent()
        agent.session_manager.get_conversation_context = AsyncMock(side_effect=RuntimeError("err"))
        result = await agent._get_conversation_context("sid1")
        assert result == ""


# ── BaseAgent._add_message_to_session ─────────────────────────────


class TestAddMessageToSession:
    def _make_agent(self):
        from agents.base_agent import BaseAgent

        class _TestAgent(BaseAgent):
            async def process(self, state):
                return state

        agent = _TestAgent(name="test_agent", role="测试", expertise=["测试"])
        agent.session_manager = AsyncMock()
        agent.logger = MagicMock()
        return agent

    @pytest.mark.asyncio
    async def test_calls_add_message(self):
        agent = self._make_agent()
        await agent._add_message_to_session("sid1", "消息", is_user=True)
        agent.session_manager.add_message.assert_called_once_with("sid1", "消息", True)

    @pytest.mark.asyncio
    async def test_exception_caught(self):
        agent = self._make_agent()
        agent.session_manager.add_message = AsyncMock(side_effect=RuntimeError("err"))
        await agent._add_message_to_session("sid1", "消息")  # 不应抛出


# ── BaseAgent._get_effective_llm ──────────────────────────────────


class TestGetEffectiveLlm:
    def _make_agent(self):
        from agents.base_agent import BaseAgent

        class _TestAgent(BaseAgent):
            async def process(self, state):
                return state

        agent = _TestAgent(name="test_agent", role="测试", expertise=["测试"])
        return agent

    def test_default_returns_main_llm(self):
        agent = self._make_agent()
        agent.llm = "main_llm"
        agent.vision_llm = "vision_llm"
        result = agent._get_effective_llm({})
        assert result == "main_llm"

    def test_multimodal_returns_vision_llm(self):
        agent = self._make_agent()
        agent.llm = "main_llm"
        agent.vision_llm = "vision_llm"
        result = agent._get_effective_llm({"has_multimodal": True})
        assert result == "vision_llm"

    def test_multimodal_no_vision_falls_back(self):
        agent = self._make_agent()
        agent.llm = "main_llm"
        agent.vision_llm = None
        result = agent._get_effective_llm({"has_multimodal": True})
        assert result == "main_llm"


# ── BaseAgent._safe_erp_query / _write_blackboard ─────────────────


class TestSafeHelpers:
    def _make_agent(self):
        from agents.base_agent import BaseAgent

        class _TestAgent(BaseAgent):
            async def process(self, state):
                return state

        agent = _TestAgent(name="test_agent", role="测试", expertise=["测试"])
        agent.erp = AsyncMock()
        agent.bb = AsyncMock()
        agent.logger = MagicMock()
        return agent

    @pytest.mark.asyncio
    async def test_safe_erp_query_success(self):
        agent = self._make_agent()

        async def _query():
            return "data"

        result = await agent._safe_erp_query(_query)
        assert result == "data"

    @pytest.mark.asyncio
    async def test_safe_erp_query_returns_fallback_on_error(self):
        agent = self._make_agent()

        async def _bad_query():
            raise RuntimeError("boom")

        result = await agent._safe_erp_query(_bad_query)
        assert "ERP" in result  # fallback message

    @pytest.mark.asyncio
    async def test_write_blackboard(self):
        agent = self._make_agent()
        await agent._write_blackboard("key", "value", ttl=60)
        agent.bb.write.assert_called_once()


# ── BillingAgent ──────────────────────────────────────────────────


class TestBillingAgent:
    def _make_agent(self):
        from agents.billing_agent import BillingAgent

        agent = BillingAgent()
        agent.llm = AsyncMock()
        agent.session_manager = AsyncMock()
        agent.bus = AsyncMock()
        agent.bb = AsyncMock()
        agent.erp = AsyncMock()
        agent.logger = MagicMock()
        return agent

    @pytest.mark.asyncio
    async def test_query_erp_with_order_id(self):
        agent = self._make_agent()
        agent.erp.query_order = AsyncMock(
            return_value=[
                {"order_id": "ORD001", "status": "已发货", "total": 199, "tracking": "SF123"}
            ]
        )

        result = await agent._query_erp("查询订单ORD001")
        assert "ORD001" in result
        assert "已发货" in result

    @pytest.mark.asyncio
    async def test_query_erp_with_customer_id(self):
        agent = self._make_agent()
        agent.erp.query_order = AsyncMock(
            return_value=[
                {"order_id": "ORD002", "status": "待付款", "total": 99, "customer_id": "C001"}
            ]
        )
        agent.erp.query_customer = AsyncMock(
            return_value={
                "name": "张三",
                "phone": "13800138000",
                "level": "VIP",
                "total_spent": 5000,
            }
        )

        result = await agent._query_erp("客户C001的订单")
        assert "张三" in result or "C001" in result

    @pytest.mark.asyncio
    async def test_query_erp_no_results(self):
        agent = self._make_agent()
        agent.erp.query_order = AsyncMock(return_value=[])

        result = await agent._query_erp("随便问")
        assert result == ""

    @pytest.mark.asyncio
    async def test_query_erp_with_customer_no_data(self):
        agent = self._make_agent()
        agent.erp.query_order = AsyncMock(
            return_value=[{"order_id": "ORD003", "status": "已完成", "total": 299}]
        )
        agent.erp.query_customer = AsyncMock(return_value=None)

        result = await agent._query_erp("订单号ORD003")
        assert "ORD003" in result

    @pytest.mark.asyncio
    async def test_process_with_erp_data(self):
        agent = self._make_agent()
        agent._safe_erp_query = AsyncMock(return_value="订单数据")
        agent._write_blackboard = AsyncMock()
        agent._process_with_llm = AsyncMock(
            return_value={
                "response": "订单已发货",
                "mode": "billing",
                "agents_used": ["billing_agent"],
            }
        )

        result = await agent.process({"customer_query": "查询订单"})
        assert result["response"] == "订单已发货"

    @pytest.mark.asyncio
    async def test_process_without_erp_data(self):
        agent = self._make_agent()
        agent._safe_erp_query = AsyncMock(return_value="")
        agent._process_with_llm = AsyncMock(
            return_value={
                "response": "无法查询",
            }
        )

        result = await agent.process({"customer_query": "查询"})
        assert "response" in result
