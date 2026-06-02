"""
响应处理智能体（v3.2 - Response Agent 增强版）
职责：
- 写入缓存（非缓存命中时）
- 会话记录写入
- SLA 监控日志
- 广播响应完成事件
- v3.2: 评估解决状态（resolved/uncertain/failed/escalated）

补齐 Router → 专家 Agent → ResponseAgent 三层架构的最后一环
"""
from typing import Dict, Any
from agents.base_agent import BaseAgent
from core.message_bus import MessageBus, Message
from core.shared_blackboard import SharedBlackboard
from session_manager import EnhancedSessionManager
from cache.response_cache import ResponseCache
from logger import get_logger

logger = get_logger("agent.response")

# 解决状态常量
RESOLUTION_RESOLVED = "resolved"
RESOLUTION_UNCERTAIN = "uncertain"
RESOLUTION_FAILED = "failed"
RESOLUTION_ESCALATED = "escalated"

# "不确定"响应的特征关键词（包含这些内容通常表示 AI 没有直接解决问题）
UNCERTAIN_PHRASES = [
    "无法确定", "无法回答", "不确定", "建议您", "请咨询",
    "请联系", "转接人工", "转人工", "稍等", "请稍候",
    "我帮不了", "抱歉无法", "无法提供",
]


class ResponseAgent(BaseAgent):
    """
    响应处理 Agent（v3.0）
    负责协作模式产出结果的后处理：缓存写入、会话记录、SLA 监控、事件广播。
    作为 LangGraph 工作流的最终节点 Agent，与 Router Agent、专家 Agent 共同构成
    Router → Expert → Response 三层状态机架构。
    """

    def __init__(
        self,
        session_manager: EnhancedSessionManager = None,
        message_bus: MessageBus = None,
        blackboard: SharedBlackboard = None,
        cache: ResponseCache = None,
    ):
        super().__init__(
            name="response_agent",
            role="响应处理专家",
            expertise=["缓存管理", "会话记录", "SLA监控", "事件广播"],
            session_manager=session_manager,
            message_bus=message_bus,
            blackboard=blackboard,
        )
        self.cache = cache

    async def process(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        ResponseAgent 核心处理流程（v3.2: 增强解决状态评估）：
        1. 评估解决状态（resolved/uncertain/failed/escalated）
        2. 写入缓存（非缓存命中时）
        3. 写入会话记录
        4. SLA 监控日志
        5. 广播响应完成事件
        """
        response = state.get("response", "")
        query = state.get("customer_query", "")
        session_id = state.get("session_id", "")
        cached = state.get("cached", False)
        mode = state.get("collaboration_mode", "sequential")
        agent = state.get("current_agent", "unknown")
        agents_used = state.get("agents_used", [])

        # 1. v3.2: 评估解决状态
        resolution_status = self._evaluate_resolution(state)
        state["resolution_status"] = resolution_status

        # 2. 写入缓存（非缓存命中时，且仅缓存确定性解决的响应）
        if response and not cached and self.cache:
            # v3.2: 仅缓存 resolved 状态的响应，避免缓存低质量回复
            if resolution_status == RESOLUTION_RESOLVED:
                try:
                    self.cache.put(query, response)
                    self.logger.debug(f"缓存写入: {query[:30]}...")
                except Exception as e:
                    self.logger.warning(f"缓存写入失败: {e}")
            else:
                self.logger.debug(f"跳过缓存写入（status={resolution_status}）: {query[:30]}...")

        # 3. 写入会话记录
        if response and session_id:
            try:
                self._add_message_to_session(session_id, response, is_user=False)
            except Exception as e:
                self.logger.warning(f"会话记录写入失败: {e}")

        # 4. SLA 监控日志
        self.logger.info(
            f"[SLA] mode={mode} agent={agent} agents_used={agents_used} "
            f"cached={cached} resolution={resolution_status}"
        )

        # 5. 广播响应完成事件
        await self._publish_event("response.complete", {
            "agent": agent,
            "mode": mode,
            "cached": cached,
            "agents_used": agents_used,
            "resolution_status": resolution_status,
        })

        return state

    def _evaluate_resolution(self, state: Dict[str, Any]) -> str:
        """
        v3.2: 基于多维信号评估解决状态
        - escalated: 升级到人工 → escalated
- failed: 空响应 / 错误降级 → failed
        - uncertain: 响应过短 / 包含不确定短语 / 低置信度路由 → uncertain
        - resolved: Agent 正常返回有效响应 → resolved
        """
        response = state.get("response", "")
        mode = state.get("collaboration_mode", "")

        # 升级场景
        if mode == "hierarchical" and state.get("query_type") == "complaint":
            return RESOLUTION_ESCALATED

        # 失败场景：空响应或错误降级
        if not response or response.strip() == "":
            return RESOLUTION_FAILED
        if response in ("处理出错，请重试", "处理出错"):
            return RESOLUTION_FAILED

        # 不确定场景：响应过短（< 15 字符，通常不是有效回答）
        if len(response.strip()) < 15:
            return RESOLUTION_UNCERTAIN

        # 不确定场景：包含"无法回答"类短语
        response_lower = response.lower()
        if any(phrase in response_lower for phrase in UNCERTAIN_PHRASES):
            return RESOLUTION_UNCERTAIN

        # 正常解决
        return RESOLUTION_RESOLVED
