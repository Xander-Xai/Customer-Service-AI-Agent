"""
技术支持专家智能体（v3.5: 增加 RAG 知识检索）
"""

from typing import Any

from .base_agent import BaseAgent

_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

请提供专业的化妆品使用指导，包括：
- 使用方法和注意事项
- 过敏/不适反应的处理建议
- 产品搭配和使用顺序
- 保存和保质期建议

回答要专业、安全、有帮助。涉及健康安全问题要谨慎。"""


class TechAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="技术支持专家",
            role="化妆品使用指导和技术问题处理",
            expertise=["使用方法", "过敏处理", "产品搭配", "保质期", "保存方法"],
        )

    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        customer_query = state["customer_query"]
        # v3.6: RAG 知识库已覆盖硬编码知识，移除冗余 _knowledge
        rag_context = await self._retrieve_knowledge(
            customer_query, collections=["tech_support", "product_knowledge"]
        )
        return await self._process_with_llm(
            state,
            self._format_system_prompt(_SYSTEM_PROMPT),
            extra_context=rag_context,
            fallback_response="抱歉，处理技术问题时遇到错误，请稍后重试。",
        )
