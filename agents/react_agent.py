"""
ReAct 推理智能体（v3.5）
结合 RAG 检索 + Function Calling 的自主推理 Agent。
实现 Thought → Action → Observation → Answer 的推理链。
"""

from typing import Any, Dict

from config import REACT_MAX_ITERATIONS
from logger import get_logger

from .base_agent import BaseAgent

logger = get_logger("agent.react")

_REACT_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

你是一个具备工具调用能力的推理代理。处理客户问题时，请按以下步骤操作：

1. **思考(Thought)**：分析客户需要什么信息，需要调用哪些工具
2. **行动(Action)**：调用合适的工具获取数据
3. **观察(Observation)**：分析工具返回的结果
4. 重复上述步骤直到获取足够信息
5. **最终回答(Answer)**：基于所有收集到的信息，给出完整、专业的回答

你拥有以下能力：
- 产品知识检索：查询产品成分、功效、适用肤质等专业知识
- ERP 系统查询：查询产品库存、订单状态、客户资料
- 技术支持：查询使用方法、过敏处理、产品搭配等技术支持信息

重要规则：
- 优先使用工具获取准确数据，不要编造信息
- 如果工具返回无结果，如实告知客户
- 最终回答要完整、专业、有帮助
"""


class ReActAgent(BaseAgent):
    """
    ReAct（Reasoning + Acting）推理智能体
    结合 RAG 知识检索和 Function Calling 工具调用，
    通过多步推理自主解决复杂客户问题。
    """

    def __init__(self, max_iterations: int = REACT_MAX_ITERATIONS):
        super().__init__(
            name="ReAct推理专家",
            role="复杂多步骤推理与工具调用",
            expertise=["多步骤推理", "RAG知识检索", "ERP工具调用", "综合分析"],
        )
        self.max_iterations = max_iterations

    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        ReAct 处理流程：
        1. RAG 检索相关知识作为上下文
        2. 使用 _process_with_tools 进行工具调用循环
        3. LLM 自主决定调用哪些工具、何时给出最终回答
        """
        customer_query = state["customer_query"]

        # Step 1: RAG 检索相关知识
        rag_context = await self._retrieve_knowledge(
            customer_query,
            collections=["product_knowledge", "faq", "tech_support"],
            n_results=5,  # ReAct 模式检索更多结果
        )

        # Step 2: 使用工具调用循环处理
        return await self._process_with_tools(
            state,
            system_prompt=self._format_system_prompt(_REACT_SYSTEM_PROMPT),
            extra_context=f"[检索知识]\n{rag_context}" if rag_context else "",
            fallback_response="抱歉，处理您的复杂问题时遇到困难。建议您提供更多细节或联系人工客服。",
            max_tool_rounds=self.max_iterations,
        )
