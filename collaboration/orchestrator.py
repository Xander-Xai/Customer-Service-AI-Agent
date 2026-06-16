"""
协作编排器（v4.3: 新增运行时模式升级 + ReAct 推理模式选择）

核心职责：
1. 根据查询复杂度、类型、关键词动态选择最佳协作模式
2. 管理5种协作模式的实例化与执行
3. 支持运行时模式升级（低质量响应自动升级到更复杂模式）

协作模式选择策略：
- 简单查询 (fast_path=True) → Sequential（顺序执行，最快）
- 投诉类 (complaint) → Hierarchical（层级协调，需要多方参与）
- 多领域/高复杂度 (>70) → ReAct（推理+行动，处理复杂问题）
- 单一领域中等复杂度 → Parallel/Consultation（并行或咨询）

架构设计：
- 单一数据源：所有模式选择逻辑集中在此类
- 解耦设计：graph节点仅调用orchestrator，不直接依赖具体模式
- 可扩展性：新增模式只需注册到_modes字典
"""

from typing import Any

from core.config import MODE_UPGRADE_ENABLED, REACT_COMPLEXITY_THRESHOLD
from core.logger import get_logger
from core.message_bus import MessageBus
from core.session.session_manager import INTENT_KEYWORDS
from core.shared_blackboard import SharedBlackboard

from .modes import ConsultationMode, HierarchicalMode, ParallelMode, ReActMode, SequentialMode

logger = get_logger("collaboration.orchestrator")

# v3.4: 从 INTENT_KEYWORDS 提取所有关键词（单一数据源）
_PRODUCT_KEYWORDS = set(INTENT_KEYWORDS.get("product_info", []))
_BILLING_KEYWORDS = set(INTENT_KEYWORDS.get("billing", [])) | set(
    INTENT_KEYWORDS.get("order_query", [])
)
_TECH_KEYWORDS = set(INTENT_KEYWORDS.get("technical_support", []))
_COMPLAINT_KEYWORDS = set(INTENT_KEYWORDS.get("complaint", []))


def _has_keywords(query: str, keywords: set) -> bool:
    """
    检查查询是否包含指定关键词集合中的任一关键词
    
    Args:
        query: 用户查询文本
        keywords: 关键词集合
        
    Returns:
        bool: 如果查询包含任一关键词则返回True
    """
    return any(kw in query for kw in keywords)


class CollaborationOrchestrator:
    """
    协作编排器（v3.0 - 唯一模式选择来源）
    
    核心职责：
    1. 统一模式选择逻辑（消除与 multi_agent_customer_service.py 的重复）
    2. graph 节点直接委托 orchestrator 执行协作
    3. 提供结构化日志和监控指标
    
    设计原则：
    - 单一职责：仅负责任务分发和模式选择
    - 开闭原则：新增模式无需修改现有代码，只需注册
    - 依赖倒置：通过MessageBus和Blackboard解耦Agent通信
    
    使用示例：
        >>> orchestrator = CollaborationOrchestrator(bus, blackboard)
        >>> mode_name, context = orchestrator.build_context(routing_result, state)
        >>> print(mode_name)  # 'parallel'
    """

    def __init__(self, message_bus: MessageBus, blackboard: SharedBlackboard):
        """
        初始化协作编排器
        
        Args:
            message_bus: 消息总线，用于Agent间异步通信
            blackboard: 共享黑板，用于状态共享和数据传递
        """
        self.bus = message_bus
        self.bb = blackboard
        self._modes = {
            "sequential": SequentialMode(bus=message_bus, bb=blackboard),
            "parallel": ParallelMode(bus=message_bus, bb=blackboard),
            "consultation": ConsultationMode(bus=message_bus, bb=blackboard),
            "hierarchical": HierarchicalMode(bus=message_bus, bb=blackboard),
            "react": ReActMode(bus=message_bus, bb=blackboard),  # v3.5
        }

    def select_mode_name(self, routing_result: Any, state: dict[str, Any]) -> str:
        """
        仅选择模式名称（供 LangGraph Conditional Edge 使用）
        
        Args:
            routing_result: 路由结果，包含复杂度、类型等信息
            state: LangGraph状态字典
            
        Returns:
            str: 选定的协作模式名称
        """
        mode_name, _ = self._select_mode(routing_result, state)
        return mode_name

    def build_context(
        self, routing_result: Any, state: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """
        选择模式并返回上下文（供 graph 节点使用）
        
        Args:
            routing_result: 路由结果
            state: LangGraph状态字典
            
        Returns:
            tuple: (mode_name, context_dict) 
                   - mode_name: 协作模式名称
                   - context_dict: 传递给模式的上下文参数
        """
        return self._select_mode(routing_result, state)

    def _select_mode(
        self, routing_result: Any, state: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """
        统一的模式选择逻辑（唯一的模式选择来源）
        
        决策流程：
        1. 快速通道判断 (< threshold) → Sequential
        2. 投诉类查询 → Hierarchical（需要协调多方）
        3. 高复杂度多领域 → ReAct（推理+行动）
        4. 其他情况根据领域数量选择 Parallel/Consultation
        
        Args:
            routing_result: 路由结果对象
                - complexity: 复杂度评分 (0-100)
                - query_type: 查询类型 (product/billing/complaint等)
                - agent_name: 主责Agent名称
                - fast_path: 是否快速通道
            state: LangGraph状态字典
                - customer_query: 用户原始查询
                
        Returns:
            tuple: (mode_name, context)
        """
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
        multi_agent_hints: list[str] = []
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

    def upgrade_mode(self, current_mode: str, state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """
        v4.3: 运行时模式升级。
        当低质量响应触发时，自动升级到更复杂的协作模式重新处理。
        升级路径：sequential → consultation → parallel → react
        """
        if not MODE_UPGRADE_ENABLED:
            return current_mode, {}

        primary_agent = state.get("current_agent", "general_agent")

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
