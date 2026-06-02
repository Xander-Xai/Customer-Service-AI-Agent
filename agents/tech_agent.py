"""
技术支持专家智能体（v3.0 - 原生异步版）
- async process() 消除死锁风险
- 深度集成 MessageBus + SharedBlackboard
- 漂移自动修复
"""
from typing import Dict, Any
from .base_agent import BaseAgent
from logger import get_logger

logger = get_logger("agent.tech_agent")

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
            expertise=["使用方法", "过敏处理", "产品搭配", "保质期", "保存方法"]
        )
        self._knowledge = {
            "过敏": "如出现过敏反应：1) 立即停用 2) 清水冲洗 3) 冰敷缓解 4) 严重时就医。建议使用前做耳后测试。",
            "保质期": "未开封保质期3年，开封后建议6-12个月内用完。请存放于阴凉避光处。",
            "搭配": "建议使用顺序：洁面→化妆水→精华→面霜→防晒。不同功效产品间隔5分钟使用。",
            "敏感": "敏感肌建议选择无酒精、无香精产品。推荐积雪草修护系列。",
        }

    async def process(self, state: Dict[str, Any]) -> Dict[str, Any]:
        customer_query = state["customer_query"]

        # 匹配知识库
        matched = [info for kw, info in self._knowledge.items() if kw in customer_query]
        if matched:
            await self._write_blackboard("tech.knowledge_matched", matched, ttl=300)

        extra_context = f"[参考知识]\n" + "\n".join(matched) if matched else ""
        system_prompt = _SYSTEM_PROMPT.format(
            self_name=self.name, self_role=self.role,
            self_expertise=", ".join(self.expertise)
        )

        return await self._process_with_llm(
            state, system_prompt,
            extra_context=extra_context,
            fallback_response="抱歉，处理技术问题时遇到错误，请稍后重试。",
        )
