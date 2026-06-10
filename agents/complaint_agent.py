"""
投诉处理专家智能体（v4.3 — RAG 增强版）
v4.3: 注入投诉处理知识库，提升首次解决率
"""

from typing import Any

from .base_agent import BaseAgent

_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

处理投诉的核心原则：
1. 首先表达真诚的歉意和理解
2. 认真倾听客户的不满，不辩解
3. 明确问题并给出具体解决方案
4. 提供合理的补偿方案
5. 跟进确认问题是否解决

如果投诉涉及质量问题或严重服务失误，建议记录并升级处理。
回答要诚恳、有温度、有担当。

示例对话：
用户：我买的面霜用了过敏，脸上起了红疹！
回答：非常抱歉给您带来这样的困扰，我完全理解您的担忧。请您先：
1. **立即停用该产品**，用清水清洁面部
2. 如果红疹严重，建议**冷敷或就医**
3. 请提供您的**订单号**，我们会立即为您处理退款或换货
4. 同时我们会将此情况反馈给品控部门进行调查

您的健康是我们最关心的，我们会全程跟进直到问题解决。"""


class ComplaintAgent(BaseAgent):
    def __init__(self, llm=None):
        super().__init__(
            name="投诉处理专家",
            role="客户投诉处理和情绪安抚",
            expertise=["投诉处理", "情绪安抚", "问题解决", "补偿方案", "升级处理"],
            llm=llm,
        )

    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        await self._write_blackboard("complaint.active", True, ttl=600)

        # v4.3: 从投诉知识库检索相关知识，注入到上下文
        extra_context = ""
        if self.knowledge_base:
            try:
                query = state.get("customer_query", "")
                extra_context = await self._retrieve_knowledge(
                    query, collections=["complaint_knowledge", "faq"], n_results=3
                )
            except Exception as e:
                self.logger.warning(f"投诉知识库检索失败: {e}")

        system_prompt = self._format_system_prompt(_SYSTEM_PROMPT)

        return await self._process_with_llm(
            state,
            system_prompt,
            extra_context=extra_context,
            fallback_response="非常抱歉给您带来不好的体验，我们会尽快为您处理。请告诉我具体问题。",
        )
