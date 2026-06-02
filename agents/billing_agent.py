"""
账单专家智能体（v3.0 - 原生异步版）
- async process() 消除死锁风险
- 深度集成 MessageBus + SharedBlackboard
- 漂移自动修复
- ERP 降级处理
"""
import re
from typing import Dict, Any
from .base_agent import BaseAgent
from logger import get_logger

logger = get_logger("agent.billing_agent")

_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

请根据客户的订单/账单问题提供专业解答：
1. 查询订单状态和物流信息
2. 说明退款/退货流程和时效
3. 解答发票和支付相关问题

回答要准确、专业，涉及金额和时间的信息要具体明确。"""


class BillingAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="账单专家",
            role="财务、订单和账单问题处理",
            expertise=["退款处理", "订单查询", "发票管理", "物流跟踪", "支付问题"]
        )

    async def process(self, state: Dict[str, Any]) -> Dict[str, Any]:
        customer_query = state["customer_query"]

        # ERP 查询（在模板方法调用前完成）
        erp_data = ""
        try:
            erp_data = await self._query_erp(customer_query)
            if erp_data:
                await self._write_blackboard("erp.order_data", erp_data, ttl=300)
        except Exception as e:
            logger.warning(f"ERP 查询失败，降级处理: {e}")
            erp_data = "[ERP 暂时不可用，请告知用户稍后再试或联系人工客服]"

        extra_context = f"[订单/财务数据]\n{erp_data}" if erp_data else ""
        system_prompt = _SYSTEM_PROMPT.format(
            self_name=self.name, self_role=self.role,
            self_expertise=", ".join(self.expertise)
        )

        return await self._process_with_llm(
            state, system_prompt,
            extra_context=extra_context,
            fallback_response="抱歉，处理账单问题时遇到系统错误，请稍后重试。",
        )

    async def _query_erp(self, query: str) -> str:
        """原生异步 ERP 查询（v3.1: 增加客户资料查询）"""
        results = []
        customer_id = None

        # 从查询中提取客户 ID（优先使用显式指定的）
        cid_match = re.search(r"C\d{3}", query)
        if cid_match:
            customer_id = cid_match.group()

        try:
            order_match = re.search(r"ORD\d+", query)
            if order_match:
                orders = await self.erp.query_order(order_id=order_match.group())
            elif customer_id:
                orders = await self.erp.query_order(customer_id=customer_id)
            else:
                orders = await self.erp.query_order()
            for o in orders[:3]:
                results.append(
                    f"订单: {o.get('order_id','')} | 状态: {o['status']} "
                    f"| 金额: {o['total']}元 | 物流: {o.get('tracking','无')}"
                )
                # 若未显式指定客户 ID，从订单结果中提取
                if not customer_id and o.get("customer_id"):
                    customer_id = o["customer_id"]
        except Exception as e:
            logger.warning(f"ERP 订单查询出错: {e}")

        # 查询客户资料
        if customer_id:
            try:
                customer = await self.erp.query_customer(customer_id)
                if customer:
                    results.append(
                        f"客户: {customer.get('name','')} | 电话: {customer.get('phone','')} "
                        f"| 等级: {customer.get('level','')} | 累计消费: {customer.get('total_spent',0)}元"
                    )
            except Exception as e:
                logger.warning(f"ERP 客户查询出错: {e}")

        return "\n".join(results) if results else ""
