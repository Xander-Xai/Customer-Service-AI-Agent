"""
投诉处理专家智能体（v6.1 — 证据缺口修复: 紧急度检测 + 升级机制）
v4.3: 注入投诉处理知识库，提升首次解决率
v6.1: 新增 detect_urgency() 紧急度检测，高紧急度自动设置 escalation_flag
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

# v6.1: 紧急度关键词 — 命中越多 scores 越高
_URGENCY_KEYWORDS = [
    "投诉", "举报", "315", "曝光", "律师", "起诉", "工商",
    "媒体", "记者", "报警", "诉讼", "法院", "监管部门", "药监局",
    "医生", "医院", "毁容", "中毒", "急诊",
]


class ComplaintAgent(BaseAgent):
    def __init__(self, llm=None):
        super().__init__(
            name="投诉处理专家",
            role="客户投诉处理和情绪安抚",
            expertise=["投诉处理", "情绪安抚", "问题解决", "补偿方案", "升级处理"],
            llm=llm,
        )

    def detect_urgency(self, text: str) -> float:
        """v6.1: 基于关键词检测投诉紧急度，返回 [0.0, 1.0] 评分。

        关键词命中数与阈值线性映射；超过 3 个独立关键词命中即为高紧急度 (≥0.8)。
        用于判断是否需要升级处理（转人工 / 上报管理层）。
        """
        if not text:
            return 0.0
        text_lower = text.lower()
        hits = sum(1 for kw in _URGENCY_KEYWORDS if kw in text_lower)
        # 3 个关键词命中 → 1.0，线性缩放
        score = min(hits / 3, 1.0)
        return score

    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        await self._write_blackboard("complaint.active", True, ttl=600)

        # v6.1: 紧急度检测 + 升级标志
        query = state.get("customer_query", "")
        urgency = self.detect_urgency(query)
        if urgency >= 0.8:
            state["escalation_flag"] = True
            self.logger.warning(
                f"投诉紧急度高 ({urgency:.1f})，设置升级标志",
                extra={"urgency": urgency, "query": query[:80]},
            )
        else:
            state["escalation_flag"] = state.get("escalation_flag", False)

        # v4.3: 从投诉知识库检索相关知识，注入到上下文
        # v6.1: 带 scene 过滤检索投诉处理相关文档
        extra_context = ""
        if self.knowledge_base:
            try:
                extra_context = await self._retrieve_knowledge(
                    query,
                    collections=["complaint_knowledge", "faq"],
                    n_results=3,
                    state=state,
                    scene="投诉处理",
                )
            except Exception as e:
                self.logger.warning(f"投诉知识库检索失败: {e}")

        # v6.1: 紧急度高时追加升级话术到系统提示
        system_prompt = self._format_system_prompt(_SYSTEM_PROMPT)
        if urgency >= 0.8:
            system_prompt += (
                "\n\n【重要】该用户情绪激动或投诉严重，请优先安抚情绪，"
                "明确告知已升级处理，并承诺专人跟进。"
                "不要拖延或推诿，直接给出明确解决方案和时限。"
            )

        return await self._process_with_llm(
            state,
            system_prompt,
            extra_context=extra_context,
            fallback_response="非常抱歉给您带来不好的体验，我们会尽快为您处理。请告诉我具体问题。",
        )
