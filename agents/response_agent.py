"""
响应处理智能体（v4.1 — 集成自我评估闭环）
职责：缓存写入、会话记录、SLA 监控、事件广播、解决状态评估 + 响应清洗 + 质量评估
"""

import re
from typing import Any

from agents.base_agent import BaseAgent
from agents.evaluator import ResponseEvaluator
from core.logger import get_logger
from core.message_bus import MessageBus
from core.shared_blackboard import SharedBlackboard

logger = get_logger("agent.response_agent")
from cache.response_cache import ResponseCache  # noqa: E402
from core.config import (  # noqa: E402
    EVAL_ALERT_ENABLED,
    EVAL_LOW_SCORE_THRESHOLD,
    EVAL_RETRY_ENABLED,
    EVAL_RETRY_THRESHOLD,
)
from core.session.session_manager import EnhancedSessionManager  # noqa: E402

# 解决状态常量
RESOLUTION_RESOLVED = "resolved"
RESOLUTION_UNCERTAIN = "uncertain"
RESOLUTION_FAILED = "failed"
RESOLUTION_ESCALATED = "escalated"

# "不确定"响应的特征关键词
UNCERTAIN_PHRASES = [
    "无法确定",
    "无法回答",
    "不确定",
    "请联系人工",
    "请咨询客服",  # 仅完整的升级短语触发（非单独"建议您"）
    "转接人工",
    "转人工",
    "稍等",
    "请稍候",
    "我帮不了",
    "抱歉无法",
    "无法提供",
]

# "升级人工"响应的特征关键词
ESCALATION_PHRASES = [
    "转接人工",
    "转人工客服",
    "人工客服介入",
    "升级处理",
    "高级客服",
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

# v4.0 安全修复: LLM 元评论清洗（仅匹配自言自语式思考，不匹配正常回复结构）
_RE_META_COMMENTARY = re.compile(
    r"^(让我分析一下|让我想想|让我来帮你查找|让我查看一下|我需要先|我来分析分析).*?[。\n]",
    re.MULTILINE,
)
_RE_AI_DISCLAIMER = re.compile(
    r"^(作为AI|作为人工智能|作为语言模型|As an AI).*?[。\n]",
    re.IGNORECASE | re.MULTILINE,
)

# v4.2 安全修复: 注入防御 — 检测系统提示泄露
# 当 LLM 回复中出现讨论"系统提示词"的内容且不含拒绝意图时，标记为注入泄露
_RE_INJECTION_DISCLOSURE = re.compile(
    r"(系统提示词?|system\s*prompt|我的指令|我的设定|内部指令|我的角色设定).{0,50}"
    r"(如下|包括|是|内容|主要|为|包含|以下|由.*组成)",
    re.IGNORECASE,
)
_INJECTION_SAFE_RESPONSE = (
    "您好！我是客服助手，很高兴为您服务。关于您的问题，我可以帮您解答产品咨询、"
    "订单查询、技术支持等相关问题。请问有什么可以帮您的吗？"
)

# v5.0: 截断检测 — 以连词/介词/未闭合括号结尾的不完整句子
_RE_TRUNCATED_ENDING = re.compile(
    r"(?:还是|而且|但是|不过|因此|所以|同时|另外|此外|以及|"
    r"或者|并且|以及|如果|虽然|即使|除非|无论|只要|"
    r"关于|对于|至于|基于|通过|除了|包括)\s*$"
)
_RE_UNCLOSED_PAREN = re.compile(r"[（\(][^）\)]*$")


def _sanitize_response(text: str) -> str:
    """
    清洗 LLM 响应内容：移除系统消息、调试代码等垃圾内容。
    v4.0: 增加 LLM 元评论清洗
    v4.2: 增加注入防御 — 检测系统提示泄露并替换为安全回复
    """
    if not text:
        return text

    # 注入防御优先：如果检测到系统提示泄露，直接返回安全回复
    if _RE_INJECTION_DISCLOSURE.search(text):
        logger.warning("检测到系统提示泄露，替换为安全回复")
        return _INJECTION_SAFE_RESPONSE

    text = _RE_SYSTEM_PREFIX.sub("", text)
    text = _RE_BARE_NUMBER.sub("", text)
    text = _RE_REACT_CREATEELEMENT.sub("", text)
    text = _RE_REACT_DANGEROUSLY.sub("", text)
    text = _RE_REACT_CONSOLE.sub("", text)
    text = _RE_REACT_JSON.sub("", text)
    text = _RE_LINE_COMMENT.sub("", text)
    text = _RE_BLOCK_COMMENT.sub("", text)
    text = _RE_PROMPT_ARTIFACT.sub("", text)
    text = _RE_META_COMMENTARY.sub("", text)
    text = _RE_AI_DISCLAIMER.sub("", text)
    text = _RE_MULTIPLE_NEWLINES.sub("\n\n", text)
    text = text.strip()

    # 截断检测：以连词/未闭合括号结尾 → 追加提示
    if text and (_RE_TRUNCATED_ENDING.search(text) or _RE_UNCLOSED_PAREN.search(text)):
        logger.warning(f"检测到疑似截断响应（末尾: ...{text[-20:]}）")
        text += "\n\n（回复可能不完整，请联系人工客服获取完整信息）"

    return text


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
        evaluator: ResponseEvaluator = None,
    ):
        super().__init__(
            name="response_agent",
            role="响应处理专家",
            expertise=["缓存管理", "会话记录", "SLA监控", "事件广播", "质量评估"],
            session_manager=session_manager,
            message_bus=message_bus,
            blackboard=blackboard,
        )
        self.cache = cache
        self.evaluator = evaluator or ResponseEvaluator()

    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        ResponseAgent 核心处理流程（v4.1: 增加质量评估闭环）：
        1. 响应清洗（移除垃圾内容）
        2. 评估解决状态
        3. 质量评估（v4.1: 自我评估闭环）
        4. 写入缓存
        5. 低分告警（v4.1）
        6. 广播响应完成事件
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

        # 2. v4.1: 质量评估（自我评估闭环）
        eval_result = self._evaluate_quality(state)
        if eval_result:
            state["eval_score"] = eval_result["score"]
            state["eval_factors"] = eval_result["factors"]
            state["eval_suggestions"] = eval_result["suggestions"]

            # 写入 SharedBlackboard 供其他 Agent / 监控读取
            await self._write_blackboard(
                f"eval:{session_id}",
                {
                    "score": eval_result["score"],
                    "factors": eval_result["factors"],
                    "agent": agent,
                    "query_type": state.get("query_type", ""),
                },
                ttl=600,
            )

            # v4.3: 低分自动重试/升级 — 评估分极低时标记需要升级
            if (
                EVAL_RETRY_ENABLED
                and eval_result["score"] < EVAL_RETRY_THRESHOLD
                and mode == "sequential"
                and not state.get("_retried")
            ):
                state["_retried"] = True
                state["_needs_upgrade"] = True
                self.logger.warning(
                    f"[EVAL] 低分触发模式升级: score={eval_result['score']} "
                    f"< threshold={EVAL_RETRY_THRESHOLD}, mode={mode}"
                )

            # 低分告警
            if eval_result["score"] < EVAL_LOW_SCORE_THRESHOLD:
                await self._alert_low_score(state, eval_result)

        # 3. 写入缓存（非缓存命中时，且仅缓存确定性解决的响应）
        if response and not cached and self.cache:
            # v3.2: 仅缓存 resolved 状态的响应，避免缓存低质量回复
            if resolution_status == RESOLUTION_RESOLVED:
                try:
                    # v6.1: 写入缓存时携带 Graph State metadata
                    # P0-02: 携带可信 user_id，个性化回答进入用户作用域；
                    #        无身份的个性化回答由 CachePolicy fail closed（不写入）。
                    cache_meta = {
                        "intent_type": state.get("query_type", "default"),
                        "user_id": state.get("user_id"),
                    }
                    self.cache.put(query, response, metadata=cache_meta)
                    self.logger.debug(f"缓存写入: {query[:30]}...")
                except Exception as e:
                    self.logger.warning(f"缓存写入失败: {e}")
            else:
                self.logger.debug(f"跳过缓存写入（status={resolution_status}）: {query[:30]}...")

        # 4. 会话记录已由专家 Agent (_process_with_llm/_process_with_tools) 写入，此处不再重复

        # 5. SLA 监控日志
        eval_score_str = f" eval={state.get('eval_score', 'N/A')}" if "eval_score" in state else ""
        self.logger.info(
            f"[SLA] mode={mode} agent={agent} agents_used={agents_used} "
            f"cached={cached} resolution={resolution_status}{eval_score_str}"
        )

        # 6. 广播响应完成事件（v4.1: 含评估分数）
        await self._publish_event(
            "response.complete",
            {
                "agent": agent,
                "mode": mode,
                "cached": cached,
                "agents_used": agents_used,
                "resolution_status": resolution_status,
                "eval_score": state.get("eval_score"),
            },
        )

        return state

    def _evaluate_resolution(self, state: dict[str, Any]) -> str:
        """
        v3.4: 基于多维信号评估解决状态（修复投诉误判为 escalated 的问题）
        - escalated: 仅当响应明确要求转人工时才标记
        - failed: 空响应 / 错误降级 → failed
        - uncertain: 响应过短 / 包含不确定短语 / 低置信度路由 → uncertain
        - resolved: Agent 正常返回有效响应 → resolved
        """
        response = state.get("response", "")

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

        # 不确定场景：响应以连词/未闭合括号结尾（截断）
        if _RE_TRUNCATED_ENDING.search(response) or _RE_UNCLOSED_PAREN.search(response):
            logger.warning("响应疑似被截断，标记为 uncertain")
            return RESOLUTION_UNCERTAIN

        # 正常解决（包括 hierarchical 模式成功处理的投诉）
        return RESOLUTION_RESOLVED

    def _evaluate_quality(self, state: dict[str, Any]) -> dict:
        """
        v4.1: 调用 ResponseEvaluator 评估回答质量
        将评估结果写入 state，供后续流程使用。
        """
        response = state.get("response", "")
        if not response:
            return None

        context = {
            "query": state.get("customer_query", ""),
            "query_type": state.get("query_type", "general"),
            "resolution_status": state.get("resolution_status", ""),
            "cached": state.get("cached", False),
        }

        try:
            return self.evaluator.evaluate(response, context)
        except Exception as e:
            self.logger.warning(f"质量评估异常: {e}")
            return None

    async def _alert_low_score(self, state: dict[str, Any], eval_result: dict):
        """v4.1: 低分回答告警"""
        if not EVAL_ALERT_ENABLED:
            return

        score = eval_result["score"]
        suggestions = eval_result.get("suggestions", [])

        alert_content = (
            f"回答质量评分偏低: {score}/100\n"
            f"会话: {state.get('session_id', 'N/A')}\n"
            f"Agent: {state.get('current_agent', 'N/A')}\n"
            f"问题类型: {state.get('query_type', 'N/A')}\n"
            f"解决状态: {state.get('resolution_status', 'N/A')}\n"
            f"改进建议: {'; '.join(suggestions[:3]) if suggestions else '无'}"
        )

        self.logger.warning(f"[EVAL] 低分告警: score={score} - {alert_content}")

        # 通过 MessageBus 广播低分告警事件
        await self._publish_event(
            "eval.low_score",
            {
                "score": score,
                "factors": eval_result.get("factors", {}),
                "suggestions": suggestions,
                "session_id": state.get("session_id", ""),
                "agent": state.get("current_agent", ""),
            },
        )
