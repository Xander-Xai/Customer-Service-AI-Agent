"""
产品专家智能体（v3.5: 增加 RAG 知识检索）
"""

from typing import Any

from .base_agent import BaseAgent

_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

请根据客户提供专业的产品信息，包括：
- 产品成分与功效分析
- 适用肤质与使用建议
- 价格与性价比
- 库存状态

回答要专业、准确。

示例对话：
用户：烟酰胺和玻尿酸可以一起用吗？
回答：可以的！烟酰胺（维生素B3）和玻尿酸是经典的搭配组合：
1. **烟酰胺**：提亮肤色、控油、收缩毛孔（建议浓度2-5%）
2. **玻尿酸**：深层保湿、修复屏障
搭配建议：先用玻尿酸精华打底，再用烟酰胺产品。注意建立耐受，初期隔天使用。

用户：敏感肌适合用什么防晒？
回答：敏感肌选择防晒建议关注以下几点：
1. **优先选择物理防晒**（氧化锌/二氧化钛），刺激性更小
2. **避免酒精、香精、化学防晒剂**（如阿伏苯宗）
3. **SPF30+ 即可**，不必追求高倍数
建议先在耳后试用48小时，无异常再上脸。"""


class ProductAgent(BaseAgent):
    def __init__(self, llm=None):
        super().__init__(
            name="产品专家",
            role="化妆品产品信息咨询和推荐",
            expertise=["产品成分", "功效分析", "价格比较", "肤质匹配", "库存查询"],
            llm=llm,
        )

    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        customer_query = state["customer_query"]

        # ERP 产品数据查询
        erp_data = await self._safe_erp_query(lambda: self._query_erp(customer_query))
        if erp_data:
            await self._write_blackboard("erp.product_data", erp_data, ttl=300)

        # v3.5: RAG 知识库检索（产品成分/功效知识）
        rag_context = await self._retrieve_knowledge(
            customer_query, collections=["product_knowledge", "faq"], state=state
        )

        # 写入产品推荐结论到黑板
        await self._write_blackboard(
            "product.recommendation",
            {
                "query": customer_query,
                "has_erp_data": bool(erp_data),
                "has_rag_context": bool(rag_context),
            },
            ttl=300,
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
