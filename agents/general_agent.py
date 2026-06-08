"""
通用咨询专家智能体（v3.4 精简版）
兼任协作协调者角色
"""
import re
from typing import Dict, Any
from .base_agent import BaseAgent

_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

你的职责：
1. 回答关于公司和产品的一般性问题
2. 引导客户找到合适的专家
3. 处理简单的售前咨询
4. 协调多Agent协作时的信息汇总

回答要友好、专业、有帮助。如果问题需要专业处理，建议转接相应专家。"""


class GeneralAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="通用咨询专家",
            role="一般咨询和客户服务协调",
            expertise=["产品概览", "服务介绍", "常见问题", "协调转接", "售前咨询"]
        )

    async def process(self, state: Dict[str, Any]) -> Dict[str, Any]:
        customer_query = state["customer_query"]

        extra_context = ""

        # 客户资料 ERP 查询
        cid_match = re.search(r"C\d{3}", customer_query)
        if cid_match:
            customer = await self._safe_erp_query(
                lambda: self.erp.query_customer(cid_match.group())
            )
            if customer and isinstance(customer, dict) and customer.get("name"):
                erp_data = (
                    f"客户: {customer.get('name','')} | 电话: {customer.get('phone','')} "
                    f"| 等级: {customer.get('level','')} | 累计消费: {customer.get('total_spent',0)}元 "
                    f"| 地址: {customer.get('address','')}"
                )
                await self._write_blackboard("erp.customer_data", erp_data, ttl=300)
                extra_context = f"[客户资料]\n{erp_data}"

        # 读取黑板上其他 Agent 的发现
        if self.bb:
            try:
                bb_data = await self.bb.read_prefix("erp.")
                if bb_data:
                    context_parts = [f"[{k}] {v}" for k, v in bb_data.items()]
                    extra_context += f"\n\n[其他Agent发现]\n" + "\n".join(context_parts[:3])
            except Exception as e:
                self.logger.debug(f"Blackboard 读取失败: {e}")

        return await self._process_with_llm(
            state, self._format_system_prompt(_SYSTEM_PROMPT),
            extra_context=extra_context,
            fallback_response="感谢您的咨询，请问有什么可以帮助您的？",
        )
