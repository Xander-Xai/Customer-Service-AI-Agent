"""
产品专家智能体（v3.5: 增加 RAG 知识检索）
"""

from typing import Any, Dict

from .base_agent import BaseAgent

_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

请根据客户提供专业的产品信息，包括：
- 产品成分与功效分析
- 适用肤质与使用建议
- 价格与性价比
- 库存状态

回答要专业、准确。"""


class ProductAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="产品专家",
            role="化妆品产品信息咨询和推荐",
            expertise=["产品成分", "功效分析", "价格比较", "肤质匹配", "库存查询"],
        )

    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        customer_query = state["customer_query"]

        # ERP 产品数据查询
        erp_data = await self._safe_erp_query(lambda: self._query_erp(customer_query))
        if erp_data:
            await self._write_blackboard("erp.product_data", erp_data, ttl=300)

        # v3.5: RAG 知识库检索（产品成分/功效知识）
        rag_context = await self._retrieve_knowledge(
            customer_query, collections=["product_knowledge", "faq"]
        )

        # 合并上下文
        context_parts = []
        if erp_data:
            context_parts.append(f"[产品数据]\n{erp_data}")
        if rag_context:
            context_parts.append(rag_context)
        extra_context = "\n\n".join(context_parts) if context_parts else ""

        return await self._process_with_llm(
            state,
            self._format_system_prompt(_SYSTEM_PROMPT),
            extra_context=extra_context,
            fallback_response="抱歉，处理产品查询时遇到问题，请稍后重试。",
        )

    async def _query_erp(self, query: str) -> str:
        """原生异步 ERP 查询"""
        results = []
        products = await self.erp.query_product(query)
        for p in products:
            results.append(
                f"产品: {p['name']} | 类别: {p['category']} | 价格: {p['price']}元 "
                f"| 规格: {p['specs']} | 成分: {p['ingredients']} | 适用: {p['suitable']}"
            )
        inventory = await self.erp.query_inventory(keyword=query)
        for inv in inventory:
            results.append(
                f"库存: {inv['product_name']} | 余量: {inv['stock']}件 | 仓库: {inv['warehouse']}"
            )
        return "\n".join(results) if results else ""
