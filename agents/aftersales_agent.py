"""
售后服务专家智能体（v6.1: 证据缺口修复 — 新售后 Agent）
负责售后支持：订单查询、物流跟踪、退换货流程
"""

from typing import Any

from .base_agent import BaseAgent

_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

你的职责包括：
1. 查询订单状态和物流信息
2. 解答退换货政策相关问题
3. 处理退款和发票事宜
4. 提供售后服务和投诉转接

回答要主动、友好，尽量一次性解决问题。

示例对话：
用户：我买的精华什么时候发货？
回答：我来帮您查一下订单信息。请问您方便提供订单号吗？
有了订单号我可以帮您查到：
1. 订单当前状态（待发货/已发货/运输中）
2. 预计发货时间
3. 物流单号和承运公司
4. 预计到达时间"""


class AftersalesAgent(BaseAgent):
    def __init__(self, llm=None):
        super().__init__(
            name="售后服务专家",
            role="售后支持和订单处理",
            expertise=["订单查询", "物流跟踪", "退换货政策", "退款处理", "发票服务"],
            llm=llm,
        )

    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        customer_query = state["customer_query"]

        # 从 RAG 检索售后支持相关文档
        rag_context = ""
        if self.knowledge_base:
            try:
                rag_context = await self._retrieve_knowledge(
                    customer_query,
                    collections=["faq", "complaint_knowledge"],
                    state=state,
                )
            except Exception as e:
                self.logger.warning(f"AftersalesAgent RAG 检索失败: {e}")

        # ERP 订单查询
        erp_data = ""
        try:
            erp_data = await self._safe_erp_query(
                lambda: self._query_erp(customer_query)
            )
        except Exception as e:
            self.logger.debug(f"AftersalesAgent ERP 查询失败: {e}")

        # 合并上下文
        context_parts = []
        if erp_data:
            context_parts.append(f"[订单信息]\n{erp_data}")
        if rag_context:
            context_parts.append(rag_context)
        extra_context = "\n\n".join(context_parts) if context_parts else ""

        # 写入售后信息到黑板
        await self._write_blackboard(
            "aftersales.info",
            {
                "query": customer_query,
                "has_erp_data": bool(erp_data),
                "has_rag_context": bool(rag_context),
            },
            ttl=300,
        )

        return await self._process_with_llm(
            state,
            self._format_system_prompt(_SYSTEM_PROMPT),
            extra_context=extra_context,
            fallback_response="抱歉，处理售后问题时遇到错误，请稍后重试。",
        )

    async def _query_erp(self, query: str) -> str:
        """查询 ERP 订单和客户信息"""
        results = []
        try:
            orders = await self.erp.query_order(query)
            if orders:
                for o in orders if isinstance(orders, list) else [orders]:
                    results.append(
                        f"订单: {o.get('order_id', '')} | 状态: {o.get('status', '')} "
                        f"| 金额: {o.get('amount', 0)}元 | 日期: {o.get('date', '')}"
                    )
        except Exception:
            pass

        try:
            customer = await self.erp.query_customer(query)
            if customer and isinstance(customer, dict) and customer.get("name"):
                c = customer
                results.append(
                    f"客户: {c.get('name', '')} | 等级: {c.get('level', '')} "
                    f"| 累计消费: {c.get('total_spent', 0)}元"
                )
        except Exception:
            pass

        return "\n".join(results) if results else ""
