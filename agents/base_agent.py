"""
基础智能体类（v3.0 - 二次开发版）
核心改造：
- process() 改为原生异步，消除 asyncio.to_thread 死锁风险
- 漂移自动修复（4 类漂移类型）
- MessageBus + SharedBlackboard 深度集成
- _process_with_llm 模板方法消除子类重复
"""
import asyncio
from typing import Dict, List, Any
from abc import ABC, abstractmethod
from langchain_core.messages import HumanMessage, SystemMessage
from session_manager import EnhancedSessionManager, DRIFT_REPAIR_STRATEGIES, DriftType
from core.message_bus import MessageBus, Message, MessageType
from core.shared_blackboard import SharedBlackboard
from erp.kingdee_adapter import KingdeeMockAdapter
from logger import get_logger


# Agent 级漂移修复指引（比通用策略更具体的操作指引），定义一次，避免每次调用重建
_AGENT_REPAIR_PROMPTS = {
    DriftType.TOPIC: "[话题漂移修复] 用户切换了话题。请先简短确认用户的新需求，"
                     "然后回答新问题。如有必要，询问用户是否还需要之前话题的解答。",
    DriftType.INTENT: "[意图漂移修复] 用户意图发生变化。请调整响应策略，"
                      "说明将从之前的模式切换到新的处理方式，确保用户了解服务变更。",
    DriftType.CONTRADICTION: "[矛盾检测修复] 检测到用户表述存在矛盾。请温和地指出矛盾点，"
                             "并请求用户确认真实需求，避免误解。",
    DriftType.REPETITION: "[重复提问修复] 用户重复提问。请参考之前的回答，"
                          "提供更精炼的回复，并主动询问是否需要更详细的解释。",
}


class BaseAgent(ABC):
    def __init__(self, name: str, role: str, expertise: List[str],
                 session_manager: EnhancedSessionManager = None,
                 message_bus: MessageBus = None,
                 blackboard: SharedBlackboard = None,
                 erp_adapter=None):
        self.name = name
        self.role = role
        self.expertise = expertise
        self.llm = None
        self.session_manager = session_manager or EnhancedSessionManager()
        self.bus = message_bus
        self.bb = blackboard
        self.erp = erp_adapter or KingdeeMockAdapter()
        self.logger = get_logger(f"agent.{name}")

    def set_llm(self, llm):
        self.llm = llm

    def set_session_manager(self, session_manager: EnhancedSessionManager):
        self.session_manager = session_manager

    def set_bus(self, bus: MessageBus):
        self.bus = bus

    def set_blackboard(self, bb: SharedBlackboard):
        self.bb = bb

    def set_erp(self, erp):
        self.erp = erp

    @abstractmethod
    async def process(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """原生异步处理（v3.0 核心改造）"""
        pass

    async def process_with_retry(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """带指数退避重试的 async process 包装（1s -> 2s -> 4s）"""
        last_exception = None
        for attempt in range(3):
            try:
                return await self.process(state)
            except Exception as e:
                last_exception = e
                delay = 1.0 * (2 ** attempt)
                self.logger.warning(f"attempt {attempt+1}/3 failed: {e}, wait {delay:.1f}s")
                if attempt < 2:
                    await asyncio.sleep(delay)
        raise last_exception

    def _get_conversation_context(self, session_id: str, max_messages: int = 6) -> str:
        try:
            conversation_context = self.session_manager.get_conversation_context(session_id, max_messages)
            if not conversation_context:
                return ""
            context_lines = []
            for msg in conversation_context:
                role = "用户" if msg.get("is_user", True) else "AI"
                content = msg.get("content", "")
                context_lines.append(f"{role}: {content}")
            return "\n".join(context_lines)
        except Exception as e:
            self.logger.error(f"获取对话上下文出错: {e}")
            return ""

    def _add_message_to_session(self, session_id: str, message: str, is_user: bool = True):
        try:
            self.session_manager.add_message(session_id, message, is_user)
        except Exception as e:
            self.logger.error(f"添加消息出错: {e}")

    def _enhance_system_prompt_with_context(self, base_prompt: str) -> str:
        return base_prompt + "\n\n重要：请结合对话历史上下文，理解客户之前的问题和需求，提供连贯、个性化的回答。保持对话的连贯性和自然性。"

    def _detect_drift(self, session_id: str, query: str) -> Dict[str, Any]:
        try:
            return self.session_manager.detect_drift(session_id, query)
        except Exception:
            return {"has_drift": False, "drifts": []}

    def _handle_drift(self, query: str, drift_result: Dict[str, Any]) -> str:
        """
        漂移自动修复（v3.1 增强版）
        基于 session_manager.DRIFT_REPAIR_STRATEGIES 生成修复提示，
        注入到 Agent 的 prompt 中。包含升级处理。
        """
        if not drift_result.get("has_drift"):
            return ""

        repairs = []

        # 漂移升级处理
        escalation = drift_result.get("escalation")
        if escalation and escalation.get("escalate"):
            repairs.append(
                f"[漂移升级提示] {escalation.get('reason', '漂移频率过高')}。"
                "请主动建议用户转接人工客服，或重新确认核心需求以聚焦对话。"
            )

        for drift in drift_result.get("drifts", []):
            drift_type = drift.get("type", "")
            # 优先使用 Agent 级指引，回退到通用策略
            repair = _AGENT_REPAIR_PROMPTS.get(drift_type) or DRIFT_REPAIR_STRATEGIES.get(drift_type, "")
            if repair:
                repairs.append(repair)

        if repairs:
            self.logger.info(f"漂移修复: {len(repairs)} 条策略已注入")
            return "\n".join(repairs)
        return ""

    async def _publish_event(self, topic: str, payload: Any):
        """发布事件到 MessageBus（v3.0: 集成到主流程）"""
        if self.bus:
            try:
                await self.bus.publish(Message(
                    msg_type=MessageType.BROADCAST, topic=topic,
                    sender=self.name, payload=payload,
                ))
            except Exception as e:
                self.logger.debug(f"事件发布失败: {e}")

    async def _write_blackboard(self, key: str, value: Any, ttl: float = 300):
        """写入 SharedBlackboard（v3.0: 带 TTL）"""
        if self.bb:
            try:
                await self.bb.write(key, value, ttl=ttl)
            except Exception as e:
                self.logger.debug(f"黑板写入失败: {e}")

    async def _process_with_llm(self, state: Dict[str, Any],
                                system_prompt: str,
                                extra_context: str = "",
                                fallback_response: str = "抱歉，处理问题时遇到错误，请稍后重试。",
                                extra_data: Any = None) -> Dict[str, Any]:
        """
        通用 LLM 处理模板方法（消除 5 个子类 process() 中的重复代码）
        子类只需提供 system_prompt 和 extra_context 即可。
        """
        customer_query = state["customer_query"]
        session_id = state.get("session_id", "default")

        self._add_message_to_session(session_id, customer_query, is_user=True)
        conversation_context = self._get_conversation_context(session_id)

        # 漂移检测 + 自动修复
        drift = self._detect_drift(session_id, customer_query)
        repair_context = self._handle_drift(customer_query, drift)

        # 发布处理开始事件
        await self._publish_event("agent.processing", {
            "agent": self.name, "query_type": state.get("query_type", "")
        })

        # 预留给子类的钩子（ERP 查询、知识库匹配等），由子类在调用前完成

        system_prompt_enhanced = self._enhance_system_prompt_with_context(system_prompt)

        messages = []
        if conversation_context:
            messages.append(SystemMessage(content=f"对话历史：\n{conversation_context}"))
        messages.append(SystemMessage(content=system_prompt_enhanced))

        # 构建用户内容
        user_content = customer_query
        if extra_context:
            user_content += f"\n\n{extra_context}"
        if repair_context:
            user_content += f"\n\n{repair_context}"
        elif drift.get("has_drift"):
            drift_info = "; ".join([d.get("detail", "") for d in drift["drifts"]])
            user_content += f"\n\n[对话漂移提示] {drift_info}"

        messages.append(HumanMessage(content=user_content))

        try:
            response = await self.llm.async_invoke(messages)
            response_content = response.content
        except Exception as e:
            self.logger.error(f"LLM 调用出错: {e}")
            response_content = fallback_response

        self._add_message_to_session(session_id, response_content, is_user=False)
        state["response"] = response_content
        state["current_agent"] = self.name

        await self._publish_event("agent.completed", {
            "agent": self.name, "response_length": len(response_content)
        })

        return state
