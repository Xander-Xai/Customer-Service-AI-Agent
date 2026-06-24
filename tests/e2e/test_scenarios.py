"""四大业务场景端到端测试。"""

import pytest

from router.query_router import QueryRouter, RoutingResult, SCENE_MAPPING

SCENARIO_CASES = [
    # (查询, 期望场景, 期望意图)
    # 售前咨询
    ("我想买一款适合敏感肌的保湿产品", "售前咨询", "product_info"),
    ("这个精华多少钱？有优惠吗？", "售前咨询", "product_info"),
    ("烟酰胺能美白吗？", "售前咨询", "cosmetic_advice"),
    # 售后支持（billing 和 order_status 都映射到售后支持）
    ("我的订单到哪了？", "售后支持", "order_status"),
    ("怎么退货？", "售后支持", "return_policy"),
    # 技术答疑
    ("用了之后过敏怎么办？", "技术答疑", "technical_support"),
    ("这个面膜怎么用？", "技术答疑", "usage_guide"),
    # 投诉处理
    ("我要投诉，产品有质量问题", "投诉处理", "complaint"),
    ("你们客服态度太差了", "投诉处理", "negative_feedback"),
    # 通用
    ("你好", "通用", "greeting"),
    ("随便看看", "通用", "general"),
]

# 向后兼容的意图映射（新意图 → 可能被路由到的旧意图）
INTENT_ALIASES = {
    "order_status": ("order_status", "billing", "order_query"),
    "return_policy": ("return_policy", "billing"),
    "recommendation": ("recommendation", "product_info", "cosmetic_advice"),
    "usage_guide": ("usage_guide", "technical_support"),
    "negative_feedback": ("negative_feedback", "complaint"),
    "greeting": ("greeting", "general"),
    "general": ("general", "greeting", "general_inquiry"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("query,expected_scene,expected_intent", SCENARIO_CASES)
async def test_scene_routing(query, expected_scene, expected_intent):
    """测试场景路由准确性"""
    router = QueryRouter()
    result: RoutingResult = await router.route(query)
    assert result.scene == expected_scene, (
        f"场景路由失败\n"
        f"查询: {query}\n"
        f"期望场景: {expected_scene}\n"
        f"实际场景: {result.scene}\n"
        f"实际意图: {result.query_type}"
    )
    # 允许向后兼容：新意图可能被路由为旧意图名称
    acceptable_intents = INTENT_ALIASES.get(expected_intent, (expected_intent,))
    assert result.query_type in acceptable_intents, (
        f"意图识别失败\n"
        f"查询: {query}\n"
        f"期望意图: {expected_intent}\n"
        f"实际意图: {result.query_type}\n"
        f"可接受: {acceptable_intents}"
    )


@pytest.mark.asyncio
async def test_scene_mapping_complete():
    """测试所有意图都有对应的场景映射"""
    from router.query_router import INTENT_CLASSES

    for intent in INTENT_CLASSES:
        assert intent in SCENE_MAPPING, f"意图 '{intent}' 缺少场景映射"
    assert len(INTENT_CLASSES) >= 10, f"意图数量不足: {len(INTENT_CLASSES)}"


@pytest.mark.asyncio
async def test_old_routing_still_works():
    """测试旧的意图类型向后兼容"""
    router = QueryRouter()
    result = await router.route("我要退款")
    assert result.query_type in ("return_policy", "billing")
    assert result.scene in ("售后支持",)