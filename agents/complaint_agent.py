"""
投诉处理专家智能体（v3.0 - 原生异步版）
- async process() 消除死锁风险
- 深度集成 MessageBus + SharedBlackboard
- 漂移自动修复
"""
from typing import Dict, Any
from .base_agent import BaseAgent
from logger import get_logger

logger = get_logger("agent.complaint_agent")

_SYSTEM_PROMPT = """你是{self_name}，专门负责{self_role}。
专业领域：{self_expertise}

处理投诉的核心原则：
1. 首先表达真诚的歉意和理解
2. 认真倾听客户的不满，不辩解
3. 明确问题并给出具体解决方案
4. 提供合理的补偿方案
5. 跟进确认问题是否解决

如果投诉涉及质量问题或严重服务失误，建议记录并升级处理。
回答要诚恳、有温度、有担当。"""


class ComplaintAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="投诉处理专家",
            role="客户投诉处理和情绪安抚",
            expertise=["投诉处理", "情绪安抚", "问题解决", "补偿方案", "升级处理"]
        )

    async def process(self, state: Dict[str, Any]) -> Dict[str, Any]:
        # 投诉处理写入黑板，供其他 Agent 参考
        await self._write_blackboard("complaint.active", True, ttl=600)

        system_prompt = _SYSTEM_PROMPT.format(
            self_name=self.name, self_role=self.role,
            self_expertise=", ".join(self.expertise)
        )

        return await self._process_with_llm(
            state, system_prompt,
            fallback_response="非常抱歉给您带来不好的体验，我们会尽快为您处理。请告诉我具体问题。",
        )
