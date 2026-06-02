"""
产品专家智能体（v3.0 - 原生异步版）
- async process() 消除死锁风险
- 深度集成 MessageBus + SharedBlackboard
- 漂移自动修复
- ERP 降级处理
"""
from typing import Dict, Any
from .base_agent import BaseAgent
from logger import get_logger

logger = get_logger("agent.product_agent")

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
            expertise=["产品成分", "功效分析", "价格比较", "肤质匹配", "库存查询"]
        )

    async def process(self, state: Dict[str, Any]) -> Dict[str, Any]:
        customer_query = state["customer_query"]

        # ERP 查询（在模板方法调用前完成）
        erp_data = ""
        try:
            erp_data = await self._query_erp(customer_query)
            if erp_data:
                await self._write_blackboard("erp.product_data", erp_data, ttl=300)
        except Exception as e:
            logger.warning(f"ERP 查询失败，降级处理: {e}")
            erp_data = "[ERP 暂时不可用，请告知用户稍后再试或提供通用产品信息]"

        extra_context = ""
        if erp_data:
            extra_context = f"[产品数据]\n{erp_data}"

        system_prompt = _SYSTEM_PROMPT.format(
            self_name=self.name, self_role=self.role,
            self_expertise=", ".join(self.expertise)
        )

        return await self._process_with_llm(
            state, system_prompt,
            extra_context=extra_context,
            fallback_response="抱歉，处理产品查询时遇到问题，请稍后重试。",
        )

    async def _query_erp(self, query: str) -> str:
        """原生异步 ERP 查询"""
        results = []
        try:
            products = await self.erp.query_product(query)
            for p in products:
                results.append(
                    f"产品: {p['name']} | 类别: {p['category']} | 价格: {p['price']}元 "
                    f"| 规格: {p['specs']} | 成分: {p['ingredients']} | 适用: {p['suitable']}"
                )
        except Exception as e:
            logger.warning(f"ERP 产品查询出错: {e}")

        try:
            inventory = await self.erp.query_inventory(keyword=query)
            for inv in inventory:
                results.append(
                    f"库存: {inv['product_name']} | 余量: {inv['stock']}件 | 仓库: {inv['warehouse']}"
                )
        except Exception as e:
            logger.warning(f"ERP 库存查询出错: {e}")

        return "\n".join(results) if results else ""
