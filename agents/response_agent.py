"""
响应处理智能体（v3.8 清洗版）
职责：缓存写入、会话记录、SLA 监控、事件广播、解决状态评估 + 响应清洗
"""
import re
from typing import Dict, Any
from agents.base_agent import BaseAgent
from core.message_bus import MessageBus
from core.shared_blackboard import SharedBlackboard
from session_manager import EnhancedSessionManager
from cache.response_cache import ResponseCache

# 解决状态常量
RESOLUTION_RESOLVED = "resolved"
RESOLUTION_UNCERTAIN = "uncertain"
RESOLUTION_FAILED = "failed"
RESOLUTION_ESCALATED = "escalated"

# "不确定"响应的特征关键词
UNCERTAIN_PHRASES = [
    "无法确定", "无法回答", "不确定", "建议您", "请咨询",
    "请联系", "转接人工", "转人工", "稍等", "请稍候",
    "我帮不了", "抱歉无法", "无法提供",
]

# "升级人工"响应的特征关键词
ESCALATION_PHRASES = [
    "转接人工", "转人工客服", "人工客服介入", "升级处理", "高级客服",
]

# ===== v3.8: 响应清洗正则 =====
# 移除 "systemsystem" 或 "system" 开头的重复内容
_RE_SYSTEM_PREFIX = re.compile(r"^(system\s*system|system)\s*", re.IGNORECASE)
# 移除数字开头的单独行（如 "1\n"）
_RE_BARE_NUMBER = re.compile(r"^\d+\s*$", re.MULTILINE)
# 移除 React/JSX 代码片段
_RE_REACT_CREATEELEMENT = re.compile(r"\.createElement\([^)]*\)[^;]*")
_RE_REACT_DANGEROUSLY = re.compile(r"dangerouslySetInnerHTML[^;]*")
_RE_REACT_CONSOLE = re.compile(r"console\.log\([^)]*\)[^;]*")
_RE_REACT_JSON = re.compile(r"JSON\.stringify[^;]*")
# 移除单行注释和块注释
_RE_LINE_COMMENT = re.compile(r"//.*$", re.MULTILINE)
_RE_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")
# 移除常见调试前缀（如 `>`, `>>>`, `<<<`）
_RE_PROMPT_ARTIFACT = re.compile(r"^[><]{2,}\s*", re.MULTILINE)
# 清理多余空行
_RE_MULTIPLE_NEWLINES = re.compile(r"\n{3,}")


def _sanitize_response(text: str) -> str:
    """
    清洗 LLM 响应内容：移除系统消息、调试代码等垃圾内容。
    """
    if not text:
        return text

    text = _RE_SYSTEM_PREFIX.sub("", text)
    text = _RE_BARE_NUMBER.sub("", text)
    text = _RE_REACT_CREATEELEMENT.sub("", text)
    text = _RE_REACT_DANGEROUSLY.sub("", text)
    text = _RE_REACT_CONSOLE.sub("", text)
    text = _RE_REACT_JSON.sub("", text)
    text = _RE_LINE_COMMENT.sub("", text)
    text = _RE_BLOCK_COMMENT.sub("", text)
    text = _RE_PROMPT_ARTIFACT.sub("", text)
    text = _RE_MULTIPLE_NEWLINES.sub("\n\n", text)
    return text.strip()


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
        ResponseAgent 核心处理流程（v3.8: 增加响应清洗）：
        1. 响应清洗（移除垃圾内容）
        2. 评估解决状态
        3. 写入缓存
        4. 广播响应完成事件
        """
        response = state.get("response", "")
        query = state.get("customer_query", "")
        session_id = state.get("session_id", "")
        cached = state.get("cached", False)
        mode = state.get("collaboration_mode", "sequential")
        agent = state.get("current_agent", "unknown")
        agents_used = state.get("agents_used", [])

        # v3.8: 清洗响应内容（移除调试代码、系统消息等）
        if response:
            cleaned = _sanitize_response(response)
            if cleaned:
                state["response"] = cleaned
                response = cleaned

        # 1. 评估解决状态
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

        # 3. 会话记录已由专家 Agent (_process_with_llm/_process_with_tools) 写入，此处不再重复

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
        v3.4: 基于多维信号评估解决状态（修复投诉误判为 escalated 的问题）
        - escalated: 仅当响应明确要求转人工时才标记
        - failed: 空响应 / 错误降级 → failed
        - uncertain: 响应过短 / 包含不确定短语 / 低置信度路由 → uncertain
        - resolved: Agent 正常返回有效响应 → resolved
        """
        response = state.get("response", "")
        mode = state.get("collaboration_mode", "")

        # 失败场景：空响应或错误降级
        if not response or response.strip() == "":
            return RESOLUTION_FAILED
        if response in ("处理出错，请重试", "处理出错"):
            return RESOLUTION_FAILED

        # v3.4: 升级场景 — 仅基于响应内容判断（而非路由模式）
        if any(phrase in response for phrase in ESCALATION_PHRASES):
            return RESOLUTION_ESCALATED

        # 不确定场景：响应过短（< 15 字符，通常不是有效回答）
        if len(response.strip()) < 15:
            return RESOLUTION_UNCERTAIN

        # 不确定场景：包含"无法回答"类短语
        response_lower = response.lower()
        if any(phrase in response_lower for phrase in UNCERTAIN_PHRASES):
            return RESOLUTION_UNCERTAIN

        # 正常解决（包括 hierarchical 模式成功处理的投诉）
        return RESOLUTION_RESOLVED
