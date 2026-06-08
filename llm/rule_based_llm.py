"""
规则引擎 LLM（开发模式降级方案）
当真实 LLM API 不可用时，基于查询类型生成基本回复
"""

import re
from typing import Any, Dict, List, Optional

from logger import get_logger

logger = get_logger("rule_llm")


# 基于关键词的回复模板
RULE_TEMPLATES = {
    "product_info": {
        "keywords": ["成分", "功效", "适合", "敏感肌", "价格", "多少钱", "产品"],
        "response": "您好！感谢您对我们产品的关注。\n\n"
        "我们的产品采用天然植物成分，经过严格的质量检测。具体产品信息建议您：\n"
        "1. 查看产品详情页面\n"
        "2. 联系我们的专业客服获取详细成分表\n"
        "3. 到专柜进行皮肤测试\n\n"
        "请问您想了解哪款产品呢？",
    },
    "billing": {
        "keywords": ["账单", "订单", "付款", "支付", "退款", "发票", "物流", "快递"],
        "response": "您好！关于您的订单/账单问题：\n\n"
        "1. **订单查询**：请提供订单号，我帮您查询状态\n"
        "2. **物流信息**：一般发货后 3-5 个工作日送达\n"
        "3. **退款流程**：7 个工作日内原路退回\n\n"
        "请您提供具体的订单号或问题描述，我来为您处理。",
    },
    "tech_support": {
        "keywords": ["使用", "方法", "步骤", "怎么用", "教程", "搭配", "顺序"],
        "response": "您好！很高兴为您提供使用指导：\n\n"
        "**基础护肤步骤**：\n"
        "1. 洁面 → 2. 爽肤水 → 3. 精华液 → 4. 乳液/面霜 → 5. 防晒（白天）\n\n"
        "**使用建议**：\n"
        "- 新产品建议先在耳后试用\n"
        "- 早晚使用不同产品效果更佳\n"
        "- 坚持使用 28 天以上见效\n\n"
        "请问您想了解哪款产品的使用方法？",
    },
    "complaint": {
        "keywords": ["投诉", "不满", "差评", "过敏", "问题", "质量"],
        "response": "非常抱歉给您带来不好的体验！🙏\n\n"
        "我们会认真对待您的反馈：\n"
        "1. 请描述具体问题（附图片更佳）\n"
        "2. 提供购买渠道和时间\n"
        "3. 我们会在 24 小时内回复处理方案\n\n"
        "您的满意度是我们的首要目标，请告诉我们如何改进。",
    },
    "general": {
        "keywords": [],
        "response": "您好！我是药妆智多星客服助手 🤖\n\n"
        "我可以帮您：\n"
        "✅ 查询产品信息和成分\n"
        "✅ 处理订单和物流问题\n"
        "✅ 提供护肤使用指导\n"
        "✅ 受理投诉和建议\n\n"
        "请问有什么可以帮助您的？",
    },
}


class RuleBasedLLM:
    """
    规则引擎 LLM（开发模式专用）
    当真实 LLM API 不可用时，基于关键词匹配生成基本回复
    """

    def __init__(self):
        self.name = "RuleBasedLLM"
        logger.info("规则引擎 LLM 初始化（开发降级模式）")

    def _classify_query(self, query: str) -> str:
        """基于关键词分类查询类型"""
        query_lower = query.lower()

        # 计算每个类别的匹配分数
        scores = {}
        for category, config in RULE_TEMPLATES.items():
            if category == "general":
                continue
            score = sum(1 for kw in config["keywords"] if kw in query_lower)
            if score > 0:
                scores[category] = score

        # 返回得分最高的类别
        if scores:
            return max(scores, key=scores.get)
        return "general"

    def _generate_response(self, query: str, query_type: str = None) -> str:
        """根据查询类型生成回复"""
        if not query_type:
            query_type = self._classify_query(query)

        template = RULE_TEMPLATES.get(query_type, RULE_TEMPLATES["general"])

        # 简单的个性化处理
        response = template["response"]

        # 提取数字（可能是订单号）
        numbers = re.findall(r"\d{10,}", query)
        if numbers:
            response += f"\n\n已记录您的订单号：{numbers[0]}，正在为您查询..."

        return response

    async def async_invoke(self, messages: list[Any], tools: Any = None) -> Any:
        """
        模拟 LLM API 调用（规则引擎实现）
        兼容 LangChain 消息格式
        """
        # 提取用户查询
        user_query = ""
        system_prompt = ""

        for msg in messages:
            msg_type = type(msg).__name__
            if msg_type == "HumanMessage":
                user_query = msg.content
            elif msg_type == "SystemMessage":
                system_prompt = msg.content

        # 从 system prompt 提取查询类型
        query_type = None
        if "产品" in system_prompt or "product" in system_prompt.lower():
            query_type = "product_info"
        elif "账单" in system_prompt or "billing" in system_prompt.lower():
            query_type = "billing"
        elif "技术" in system_prompt or "tech" in system_prompt.lower():
            query_type = "tech_support"
        elif "投诉" in system_prompt or "complaint" in system_prompt.lower():
            query_type = "complaint"

        # 生成回复
        response_content = self._generate_response(user_query, query_type)

        # 构造兼容的响应对象
        class MockResponse:
            def __init__(self, content, tool_calls=None):
                self.content = content
                self.tool_calls = tool_calls or []

        return MockResponse(response_content)

    def invoke(self, messages: list[Any], tools: Any = None) -> Any:
        """同步版本（兼容性）"""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            raise RuntimeError("Use async_invoke() in async context")
        except RuntimeError as e:
            if "no running" in str(e) or "Use async" in str(e):
                return asyncio.run(self.async_invoke(messages, tools))
            raise
