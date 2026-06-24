"""
销售专家智能体（v6.1: 证据缺口修复 — 新产品推荐 Agent）
负责售前咨询：产品推荐、成分分析、肤质匹配
"""

from typing import Any

from .base_agent import BaseAgent

_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

你的职责包括：
1. 了解客户的肤质、需求和预算
2. 推荐合适的产品组合
3. 解释产品成分与功效
4. 回答价格和优惠相关问题

回答要专业、有同理心，基于客户的具体情况推荐。

示例对话：
用户：我油性皮肤，想要控油的产品
回答：油性皮肤控油建议从以下几方面入手：
1. **清洁**：选择含氨基酸表活的洁面，避免过度清洁
2. **化妆水**：含烟酰胺或金缕梅成分的控油化妆水
3. **精华**：水杨酸精华可以帮助疏通毛孔、减少出油
4. **面霜**：选择清爽型乳液或凝胶质地
请问您的皮肤是否敏感？有没有特别需要改善的问题？"""


class SalesAgent(BaseAgent):
    def __init__(self, llm=None):
        super().__init__(
            name="销售专家",
            role="售前咨询和产品推荐",
            expertise=["产品推荐", "肤质匹配", "成分分析", "价格咨询", "优惠活动"],
            llm=llm,
        )

    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        customer_query = state["customer_query"]

        # 从 RAG 检索售前咨询相关文档（带 scene 过滤）
        rag_context = ""
        if self.knowledge_base:
            try:
                rag_context = await self._retrieve_knowledge(
                    customer_query,
                    collections=["product_knowledge", "faq"],
                    state=state,
                )
            except Exception as e:
                self.logger.warning(f"SalesAgent RAG 检索失败: {e}")

        # 写入销售推荐信息到黑板
        await self._write_blackboard(
            "sales.recommendation",
            {
                "query": customer_query,
                "has_rag_context": bool(rag_context),
            },
            ttl=300,
        )

        return await self._process_with_llm(
            state,
            self._format_system_prompt(_SYSTEM_PROMPT),
            extra_context=rag_context,
            fallback_response="抱歉，处理销售咨询时遇到问题，请稍后重试。",
        )
