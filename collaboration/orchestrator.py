"""
协作编排器（v3.0 - 统一版）
核心改造：
- 统一模式选择逻辑（消除与 multi_agent_customer_service.py 的重复）
- graph 节点直接委托 orchestrator
- 结构化日志
"""
from typing import Any, Dict, Tuple, List
from .modes import SequentialMode, ParallelMode, ConsultationMode, HierarchicalMode
from core.message_bus import MessageBus
from core.shared_blackboard import SharedBlackboard
from session_manager import INTENT_KEYWORDS
from logger import get_logger

logger = get_logger("collaboration.orchestrator")

# 从 INTENT_KEYWORDS 提取多 Agent 路由关键词（单一数据源，避免与 session_manager 重复）
_PRODUCT_KEYWORDS = set(INTENT_KEYWORDS.get("product_info", []))
_BILLING_KEYWORDS = set(INTENT_KEYWORDS.get("billing", [])) | set(INTENT_KEYWORDS.get("order_query", []))
_TECH_KEYWORDS = set(INTENT_KEYWORDS.get("technical_support", []))


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
            if _has_keywords(query, {"产品", "质量"}):
                sub_tasks["product_agent"] = f"[辅助] 检查产品相关信息：{query}"
            if _has_keywords(query, {"退款", "订单"}):
                sub_tasks["billing_agent"] = f"[辅助] 检查订单/退款信息：{query}"
            return "hierarchical", {"coordinator": "general_agent", "sub_tasks": sub_tasks}

        # 涉及多领域 → 并行模式
        multi_agent_hints: List[str] = []
        if _has_keywords(query, _PRODUCT_KEYWORDS):
            multi_agent_hints.append("product_agent")
        if _has_keywords(query, _BILLING_KEYWORDS):
            multi_agent_hints.append("billing_agent")
        if _has_keywords(query, _TECH_KEYWORDS):
            multi_agent_hints.append("tech_agent")

        if len(multi_agent_hints) >= 2:
            return "parallel", {"agent_list": multi_agent_hints}

        # 需要专业补充 → 咨询模式
        consult_map = {
            "product_agent": ["tech_agent"],
            "billing_agent": ["product_agent"],
            "tech_agent": ["product_agent"],
        }
        consultees = consult_map.get(primary_agent, [])
        if consultees and complexity >= 60:
            return "consultation", {
                "primary_agent": primary_agent,
                "consult_agents": consultees,
            }

        # 默认：顺序模式
        return "sequential", {"primary_agent": primary_agent}
