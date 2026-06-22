"""
ReAct 推理智能体（v3.5）
结合 RAG 检索 + Function Calling 的自主推理 Agent。
实现 Thought → Action → Observation → Answer 的推理链。
"""

from typing import Any

from core.config import REACT_MAX_ITERATIONS
from core.logger import get_logger

from .base_agent import BaseAgent

logger = get_logger("agent.react")

_REACT_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

你是一个具备工具调用能力的推理代理。处理客户问题时，请严格按以下推理链操作：

**推理链格式（Chain-of-Thought）：**
1. **Thought**：分析客户需要什么信息，我应该调用什么工具
2. **Action**：调用合适的工具获取数据（产品查询/订单查询/知识检索）
3. **Observation**：分析工具返回的结果，判断信息是否充足
4. 重复 Thought → Action → Observation 直到获取足够信息
5. **Final Thought**：我已经收集到足够信息，可以给出完整回答
6. **Answer**：基于所有收集到的信息，给出完整、专业的回答

你拥有以下能力：
- 产品知识检索：查询产品成分、功效、适用肤质等专业知识
- ERP 系统查询：查询产品库存、订单状态、客户资料
- 技术支持：查询使用方法、过敏处理、产品搭配等技术支持信息

重要规则：
- 优先使用工具获取准确数据，不要编造信息
- 如果工具返回无结果，如实告知客户
- 每次工具调用后，评估信息是否充足，避免不必要的重复调用
- 最终回答要完整、专业、有帮助

**推理示例：**
用户：我想查一下订单 C20240601 的物流状态，另外烟酰胺精华适合干皮吗？
Thought: 用户有两个问题：1) 查订单物流 2) 烟酰胺精华是否适合干皮。我需要先查订单，再查产品知识。
Action: [调用 query_order 工具，参数 order_id="C20240601"]
Observation: 订单已发货，快递单号 SF1234567，预计明天到达。
Thought: 订单信息已获取。现在需要查烟酰胺精华的适用肤质信息。
Action: [调用 RAG 检索 product_knowledge，查询"烟酰胺精华 干皮"]
Observation: 烟酰胺精华适合油性和混合性肌肤，干皮建议搭配保湿产品使用。
Thought: 两个问题的信息都已充足，可以给出最终回答。
Answer: 关于您的订单 C20240601，已发货，快递单号 SF1234567，预计明天到达。
关于烟酰胺精华，它更适合油性和混合性肌肤。如果您是干皮，建议搭配保湿精华或面霜一起使用，避免单独使用导致干燥。
"""


class ReActAgent(BaseAgent):
    """
    ReAct（Reasoning + Acting）推理智能体
    结合 RAG 知识检索和 Function Calling 工具调用，
    通过多步推理自主解决复杂客户问题。
    """

    def __init__(self, max_iterations: int = REACT_MAX_ITERATIONS, llm=None):
        super().__init__(
            name="ReAct推理专家",
            role="复杂多步骤推理与工具调用",
            expertise=["多步骤推理", "RAG知识检索", "ERP工具调用", "综合分析"],
            llm=llm,
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
            state=state,
        )

        # Step 2: 使用工具调用循环处理
        return await self._process_with_tools(
            state,
            system_prompt=self._format_system_prompt(_REACT_SYSTEM_PROMPT),
            extra_context=f"[检索知识]\n{rag_context}" if rag_context else "",
            fallback_response="抱歉，处理您的复杂问题时遇到困难。建议您提供更多细节或联系人工客服。",
            max_tool_rounds=self.max_iterations,
        )
