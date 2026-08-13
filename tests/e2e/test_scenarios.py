"""四大业务场景端到端测试（v6.1 扩展版 — 40+ 覆盖用例）。"""

import pytest

from router.query_router import QueryRouter, RoutingResult, SCENE_MAPPING

# 40+ 条场景覆盖测试用例
SCENARIO_CASES = [
    # ========== 售前咨询 (12 条) ==========
    ("我想买一款适合敏感肌的保湿产品", "售前咨询", "product_info"),
    ("这个精华多少钱？有优惠吗？", "售前咨询", "product_info"),
    ("你们家哪款面霜保湿效果好？", "售前咨询", "product_info"),
    ("请问你们有含烟酰胺的精华吗？", "售前咨询", "product_info"),
    ("这款产品的成分是什么？", "售前咨询", "product_info"),
    ("烟酰胺能美白吗？", "售前咨询", "cosmetic_advice"),
    ("油皮适合用什么护肤品？", "售前咨询", "cosmetic_advice"),
    ("干性肌肤用什么保湿成分最好？", "售前咨询", "cosmetic_advice"),
    ("敏感肌可以用视黄醇吗？", "售前咨询", "cosmetic_advice"),
    ("你们家有什么抗衰老产品推荐？", "售前咨询", "recommendation"),
    ("我想送妈妈一套护肤品，有什么推荐？", "售前咨询", "recommendation"),
    ("孕妇能用你们的美白产品吗？", "售前咨询", "recommendation"),

    # ========== 售后支持 (10 条) ==========
    ("我的订单到哪了？", "售后支持", "order_status"),
    ("怎么退货？", "售后支持", "return_policy"),
    ("还没发货能取消订单吗？", "售后支持", "order_status"),
    ("退款多久到账？", "售后支持", "return_policy"),
    ("我要退款，产品质量有问题", "投诉处理", "complaint"),
    ("物流显示签收了我没收到货", "售后支持", "order_status"),
    ("可以换货吗？怎么操作？", "售后支持", "return_policy"),
    ("订单少发了一件商品", "售后支持", "order_status"),
    ("怎么查物流信息？", "售后支持", "order_status"),
    ("退货的运费谁出？", "售后支持", "return_policy"),

    # ========== 技术答疑 (10 条) ==========
    ("用了之后过敏怎么办？", "技术答疑", "technical_support"),
    ("这个面膜怎么用？", "技术答疑", "usage_guide"),
    ("VC和烟酰胺能一起用吗？", "技术答疑", "technical_support"),
    ("使用顺序是先用精华还是先用乳液？", "技术答疑", "usage_guide"),
    ("产品保质期多久？", "技术答疑", "technical_support"),
    ("含视黄醇的产品需要避光用吗？", "技术答疑", "technical_support"),
    ("刷酸后怎么护理？", "技术答疑", "technical_support"),
    ("早C晚A具体怎么搭配？", "技术答疑", "usage_guide"),
    ("用了A醇脱皮正常吗？", "技术答疑", "technical_support"),
    ("不同分子量玻尿酸有什么区别？", "技术答疑", "technical_support"),

    # ========== 投诉处理 (8 条) ==========
    ("我要投诉，产品有质量问题", "投诉处理", "complaint"),
    ("你们客服态度太差了", "投诉处理", "negative_feedback"),
    ("产品用了过敏，我要投诉！", "投诉处理", "complaint"),
    ("收到的面霜包装破损了", "投诉处理", "complaint"),
    ("发错货了！我要买精华发成了面霜", "投诉处理", "complaint"),
    ("你们虚假宣传，根本没有效果", "投诉处理", "complaint"),
    ("我要找你们经理投诉！", "投诉处理", "negative_feedback"),
    ("投诉处理需要多长时间？", "投诉处理", "complaint"),

    # ========== 通用对话 (6 条) ==========
    ("你好", "通用", "greeting"),
    ("您好，有人吗？", "通用", "greeting"),
    ("随便看看", "通用", "general"),
    ("谢谢", "通用", "general"),
    ("在吗？", "通用", "greeting"),
    ("Hi", "通用", "greeting"),
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
    "cosmetic_advice": ("cosmetic_advice", "product_info", "recommendation"),
    "product_info": ("product_info", "cosmetic_advice"),
    "complaint": ("complaint", "negative_feedback"),
    "technical_support": ("technical_support", "usage_guide"),
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


@pytest.mark.asyncio
async def test_complaint_urgency_detection():
    """测试投诉紧急度检测"""
    from agents.complaint_agent import ComplaintAgent

    agent = ComplaintAgent()
    high_urgency = agent.detect_urgency("我要投诉你们，我要起诉！找315曝光！")
    assert high_urgency >= 0.8, f"高紧急度投诉检测失败: {high_urgency}"
    low_urgency = agent.detect_urgency("我想咨询一下退款事宜")
    assert low_urgency < 0.8, f"低紧急度投诉误报: {low_urgency}"