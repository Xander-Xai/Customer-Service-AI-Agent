"""
协作编排器（v4.3: 新增运行时模式升级 + ReAct 推理模式选择）
核心改造：
- 统一模式选择逻辑（消除与 multi_agent_customer_service.py 的重复）
- graph 节点直接委托 orchestrator
- 结构化日志
- v3.5: 高复杂度多领域查询路由到 ReAct 模式
- v4.3: 运行时模式升级（低质量响应自动升级到更复杂模式）
"""
from typing import Any, Dict, Tuple, List
from .modes import SequentialMode, ParallelMode, ConsultationMode, HierarchicalMode, ReActMode
from core.message_bus import MessageBus
from core.shared_blackboard import SharedBlackboard
from session_manager import INTENT_KEYWORDS
from config import REACT_COMPLEXITY_THRESHOLD, MODE_UPGRADE_ENABLED
from logger import get_logger

logger = get_logger("collaboration.orchestrator")

# v3.4: 从 INTENT_KEYWORDS 提取所有关键词（单一数据源）
_PRODUCT_KEYWORDS = set(INTENT_KEYWORDS.get("product_info", []))
_BILLING_KEYWORDS = set(INTENT_KEYWORDS.get("billing", [])) | set(INTENT_KEYWORDS.get("order_query", []))
_TECH_KEYWORDS = set(INTENT_KEYWORDS.get("technical_support", []))
_COMPLAINT_KEYWORDS = set(INTENT_KEYWORDS.get("complaint", []))


def _has_keywords(query: str, keywords: set) -> bool:
    """检查查询是否包含指定关键词集合中的任一关键词"""
    return any(kw in query for kw in keywords)


class CollaborationOrchestrator:
    """
    协作编排器（v3.0 - 唯一模式选择来源）
    所有协作模式选择逻辑统一在此处，graph 节点通过本类执行
    """

    def __init__(self, message_bus: MessageBus, blackboard: SharedBlackboard):
        self.bus = message_bus
        self.bb = blackboard
        self._modes = {
            "sequential": SequentialMode(bus=message_bus, bb=blackboard),
            "parallel": ParallelMode(bus=message_bus, bb=blackboard),
            "consultation": ConsultationMode(bus=message_bus, bb=blackboard),
            "hierarchical": HierarchicalMode(bus=message_bus, bb=blackboard),
            "react": ReActMode(bus=message_bus, bb=blackboard),  # v3.5
        }

    def select_mode_name(self, routing_result: Any, state: Dict[str, Any]) -> str:
        """仅选择模式名称（供 LangGraph Conditional Edge 使用）"""
        mode_name, _ = self._select_mode(routing_result, state)
        return mode_name

    def build_context(self, routing_result: Any, state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """选择模式并返回 (mode_name, context)（供 graph 节点使用）"""
        return self._select_mode(routing_result, state)

    def _select_mode(self, routing_result: Any, state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """统一的模式选择逻辑（唯一的模式选择来源）"""
        complexity = routing_result.complexity
        query_type = routing_result.query_type
        primary_agent = routing_result.agent_name
        query = state.get("customer_query", "")

        # 快速通道 (< threshold): Sequential
        if routing_result.fast_path:
            return "sequential", {"primary_agent": primary_agent}

        # 投诉 → 层次模式（协调者 + 各方协作）
        if query_type == "complaint":
            sub_tasks = {"complaint_agent": query}
            # v3.4: 使用 INTENT_KEYWORDS 而非硬编码关键词
            if _has_keywords(query, _PRODUCT_KEYWORDS):
                sub_tasks["product_agent"] = f"[辅助] 检查产品相关信息：{query}"
            if _has_keywords(query, _BILLING_KEYWORDS):
                sub_tasks["billing_agent"] = f"[辅助] 检查订单/退款信息：{query}"
            return "hierarchical", {"coordinator": "general_agent", "sub_tasks": sub_tasks}

        # 涉及多领域 → 并行模式 或 ReAct 推理模式
        multi_agent_hints: List[str] = []
        if _has_keywords(query, _PRODUCT_KEYWORDS):
            multi_agent_hints.append("product_agent")
        if _has_keywords(query, _BILLING_KEYWORDS):
            multi_agent_hints.append("billing_agent")
        if _has_keywords(query, _TECH_KEYWORDS):
            multi_agent_hints.append("tech_agent")

        # v3.5: 高复杂度 + 多领域 → ReAct 推理模式（优先于简单并行）
        if complexity >= REACT_COMPLEXITY_THRESHOLD and len(multi_agent_hints) >= 2:
            return "react", {"primary_agent": primary_agent}

        if len(multi_agent_hints) >= 2:
            return "parallel", {"agent_list": multi_agent_hints}

        # 需要专业补充 → 咨询模式
        consult_map = {
            "product_agent": ["tech_agent"],
            "billing_agent": ["product_agent"],
            "tech_agent": ["product_agent"],
        }
        consultees = consult_map.get(primary_agent, [])
        if consultees and complexity >= REACT_COMPLEXITY_THRESHOLD:
            return "consultation", {
                "primary_agent": primary_agent,
                "consult_agents": consultees,
            }

        # 默认：顺序模式
        return "sequential", {"primary_agent": primary_agent}

    def upgrade_mode(self, current_mode: str, state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """
        v4.3: 运行时模式升级。
        当低质量响应触发时，自动升级到更复杂的协作模式重新处理。
        升级路径：sequential → consultation → parallel → react
        """
        if not MODE_UPGRADE_ENABLED:
            return current_mode, {}

        primary_agent = state.get("current_agent", "general_agent")
        query = state.get("customer_query", "")

        # 升级路径映射
        upgrade_map = {
            "sequential": "consultation",
            "consultation": "parallel",
            "parallel": "react",
        }

        new_mode = upgrade_map.get(current_mode)
        if not new_mode or new_mode == current_mode:
            # 已是最复杂模式，无法继续升级
            logger.info(f"[ModeUpgrade] {current_mode} 已是最复杂模式，跳过升级")
            return current_mode, {}

        # 构建升级后的上下文
        if new_mode == "consultation":
            consult_map = {
                "product_agent": ["tech_agent"],
                "billing_agent": ["product_agent"],
                "tech_agent": ["product_agent"],
                "complaint_agent": ["product_agent"],
                "general_agent": ["product_agent"],
            }
            consultees = consult_map.get(primary_agent, ["product_agent"])
            context = {"primary_agent": primary_agent, "consult_agents": consultees}
        elif new_mode == "parallel":
            # 并行模式：添加相关 Agent
            agent_list = [primary_agent]
            if primary_agent != "product_agent":
                agent_list.append("product_agent")
            if primary_agent != "tech_agent":
                agent_list.append("tech_agent")
            context = {"agent_list": agent_list[:3]}
        elif new_mode == "react":
            context = {"primary_agent": primary_agent}
        else:
            context = {"primary_agent": primary_agent}

        logger.info(f"[ModeUpgrade] {current_mode} → {new_mode} (agent={primary_agent})")
        return new_mode, context
