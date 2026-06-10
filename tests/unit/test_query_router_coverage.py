"""
测试 router/query_router.py — 双层路由器覆盖率补齐
覆盖：RoutingResult、INTENT_AGENT_MAP、_rule_classify_and_score、_llm_classify、route
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from router.query_router import INTENT_AGENT_MAP, QueryRouter, RoutingResult


class TestRoutingResult:

    def test_default_values(self):
        r = RoutingResult()
        assert r.query_type == "general_inquiry"
        assert r.agent_name == "general_agent"
        assert r.complexity == 0
        assert r.fast_path is True
        assert r.confidence == 0.0

    def test_custom_values(self):
        r = RoutingResult(
            query_type="complaint",
            agent_name="complaint_agent",
            complexity=80,
            fast_path=False,
            confidence=0.9,
        )
        assert r.query_type == "complaint"
        assert r.complexity == 80


class TestIntentAgentMap:

    def test_all_intents_mapped(self):
        for intent in [
            "product_info",
            "technical_support",
            "billing",
            "complaint",
            "general_inquiry",
            "order_query",
            "cosmetic_advice",
        ]:
            assert intent in INTENT_AGENT_MAP

    def test_product_maps_to_product_agent(self):
        assert INTENT_AGENT_MAP["product_info"] == "product_agent"

    def test_complaint_maps_to_complaint_agent(self):
        assert INTENT_AGENT_MAP["complaint"] == "complaint_agent"


class TestRuleClassifyAndScore:

    def setup_method(self):
        self.router = QueryRouter()

    def test_product_info(self):
        intent, scores, complexity = self.router._rule_classify_and_score("这个面霜多少钱")
        assert intent == "product_info"
        assert "product_info" in scores

    def test_technical_support(self):
        intent, scores, complexity = self.router._rule_classify_and_score("过敏了怎么办")
        assert intent == "technical_support"

    def test_billing(self):
        intent, scores, complexity = self.router._rule_classify_and_score("我要退款")
        assert intent == "billing"

    def test_complaint(self):
        intent, scores, complexity = self.router._rule_classify_and_score("投诉你们客服态度差")
        assert intent == "complaint"

    def test_order_query(self):
        intent, scores, complexity = self.router._rule_classify_and_score("我的快递到货了吗")
        assert intent in ("order_query", "billing")  # 两个模式都匹配

    def test_cosmetic_advice(self):
        intent, scores, complexity = self.router._rule_classify_and_score("我是油性肤质怎么护肤")
        assert intent == "cosmetic_advice"

    def test_no_match_returns_none(self):
        intent, scores, complexity = self.router._rule_classify_and_score("今天天气真好")
        assert intent is None
        assert scores == {}

    def test_complexity_long_query(self):
        """长查询增加复杂度"""
        _, _, short_c = self.router._rule_classify_and_score("短问")
        _, _, long_c = self.router._rule_classify_and_score("这是一个" + "很长" * 100 + "的查询问题")
        assert long_c > short_c

    def test_complexity_multi_intent(self):
        """多意图增加复杂度"""
        _, _, c1 = self.router._rule_classify_and_score("产品")
        _, _, c2 = self.router._rule_classify_and_score("产品退款投诉")
        assert c2 >= c1

    def test_complexity_complaint_bonus(self):
        """投诉意图增加额外复杂度"""
        _, _, c_normal = self.router._rule_classify_and_score("产品")
        _, _, c_complaint = self.router._rule_classify_and_score("投诉态度差")
        assert c_complaint > c_normal

    def test_complexity_price_bonus(self):
        """价格关键词增加复杂度"""
        _, _, c = self.router._rule_classify_and_score("这个多少钱 ¥199")
        assert c >= 15  # 价格加分

    def test_complexity_marks_bonus(self):
        """问号感叹号增加复杂度"""
        _, _, c = self.router._rule_classify_and_score("这是什么？！怎么样？")
        assert c >= 10  # 标点加分

    def test_complexity_long_context(self):
        """长上下文增加复杂度"""
        _, _, c1 = self.router._rule_classify_and_score("产品", "")
        _, _, c2 = self.router._rule_classify_and_score("产品", "x" * 600)
        assert c2 >= c1

    def test_complexity_capped_at_100(self):
        """复杂度上限 100"""
        huge_query = "投诉退款过敏产品" * 50 + "？！" * 10
        _, _, c = self.router._rule_classify_and_score(huge_query, "x" * 1000)
        assert c <= 100

    def test_complaint_priority_over_product(self):
        """投诉优先级高于产品"""
        intent, _, _ = self.router._rule_classify_and_score("产品投诉态度差")
        assert intent == "complaint"

    def test_tech_terms_complexity(self):
        """技术术语增加复杂度"""
        _, _, c = self.router._rule_classify_and_score("过敏成分配方")
        assert c >= 10


class TestLlmClassify:

    def setup_method(self):
        self.router = QueryRouter()

    @pytest.mark.asyncio
    async def test_no_llm_returns_default(self):
        result = await self.router._llm_classify("你好")
        assert result["query_type"] == "general_inquiry"
        assert result["confidence"] == 0.5

    @pytest.mark.asyncio
    async def test_llm_valid_json_response(self):
        mock_response = MagicMock()
        mock_response.content = '{"query_type": "product_info", "confidence": 0.9, "reason": "产品咨询"}'
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(return_value=mock_response)
        self.router.llm = mock_llm

        result = await self.router._llm_classify("这个面霜怎么样")
        assert result["query_type"] == "product_info"
        assert result["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_llm_json_with_prefix_text(self):
        mock_response = MagicMock()
        mock_response.content = '分类结果: {"query_type": "billing", "confidence": 0.8}'
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(return_value=mock_response)
        self.router.llm = mock_llm

        result = await self.router._llm_classify("退款")
        assert result["query_type"] == "billing"

    @pytest.mark.asyncio
    async def test_llm_non_json_fallback(self):
        mock_response = MagicMock()
        mock_response.content = "这是一个产品咨询"
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(return_value=mock_response)
        self.router.llm = mock_llm

        result = await self.router._llm_classify("产品咨询")
        assert result["query_type"] == "general_inquiry"

    @pytest.mark.asyncio
    async def test_llm_exception_returns_error(self):
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(side_effect=RuntimeError("API down"))
        self.router.llm = mock_llm

        result = await self.router._llm_classify("查询")
        assert result["query_type"] == "general_inquiry"
        assert result["confidence"] == 0.1
        assert result["raw"] == "llm_error"

    @pytest.mark.asyncio
    async def test_llm_with_context(self):
        mock_response = MagicMock()
        mock_response.content = '{"query_type": "product_info", "confidence": 0.85}'
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(return_value=mock_response)
        self.router.llm = mock_llm

        result = await self.router._llm_classify("产品", context="之前的对话")
        mock_llm.async_invoke.assert_called_once()


class TestRoute:

    def setup_method(self):
        self.router = QueryRouter()

    @pytest.mark.asyncio
    async def test_route_no_llm_uses_rule(self):
        result = await self.router.route("退款订单")
        assert result.query_type in ("billing", "order_query")
        assert isinstance(result, RoutingResult)

    @pytest.mark.asyncio
    async def test_route_with_llm_agreement(self):
        mock_response = MagicMock()
        mock_response.content = '{"query_type": "product_info", "confidence": 0.9}'
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(return_value=mock_response)
        self.router.llm = mock_llm

        result = await self.router.route("这个面霜怎么样")
        assert result.query_type == "product_info"

    @pytest.mark.asyncio
    async def test_route_rule_override_low_confidence(self):
        """LLM 低置信度时规则覆盖"""
        mock_response = MagicMock()
        mock_response.content = '{"query_type": "general_inquiry", "confidence": 0.3}'
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(return_value=mock_response)
        self.router.llm = mock_llm

        result = await self.router.route("退款")
        assert result.query_type == "billing"  # 规则覆盖 LLM
        assert result.rule_override is True

    @pytest.mark.asyncio
    async def test_route_llm_high_confidence_no_override(self):
        """LLM 高置信度时规则不覆盖"""
        mock_response = MagicMock()
        mock_response.content = '{"query_type": "complaint", "confidence": 0.95}'
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(return_value=mock_response)
        self.router.llm = mock_llm

        result = await self.router.route("退款")
        assert result.query_type == "complaint"  # LLM 高置信度胜出
        assert result.rule_override is False

    @pytest.mark.asyncio
    async def test_route_fast_path(self):
        result = await self.router.route("你好")
        assert result.fast_path is True  # 简单查询低于阈值

    @pytest.mark.asyncio
    async def test_route_agent_name_mapping(self):
        result = await self.router.route("退款")
        assert result.agent_name == "billing_agent"

    @pytest.mark.asyncio
    async def test_route_unknown_intent_maps_to_general(self):
        mock_response = MagicMock()
        mock_response.content = '{"query_type": "unknown_type", "confidence": 0.9}'
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(return_value=mock_response)
        self.router.llm = mock_llm

        result = await self.router.route("随便问")
        assert result.agent_name == "general_agent"  # 未知意图回退到通用
