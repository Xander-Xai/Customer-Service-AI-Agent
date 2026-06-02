"""
多智能体客服系统（v3.0 二次开发版）
LangGraph 状态机 + 双层路由 + 4种协作模式 + 二级缓存 + 通信总线

v3.0 核心改造：
- 全部图节点原生异步，消除 asyncio.to_thread 死锁风险
- 模式选择逻辑统一委托 orchestrator（消除重复代码）
- 结构化日志替换 print
- 性能指标实时采集
"""
import time
import asyncio
from typing import Dict, List, TypedDict
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END

load_dotenv()
from config import (
    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL,
    ROUTING_COMPLEXITY_THRESHOLD, CACHE_L1_MAX, CACHE_L2_MAX, CACHE_TTL,
    SESSION_STORAGE_BACKEND, SESSION_WINDOW_SIZE, REDIS_URL, ERP_MODE,
)

from agents import ProductAgent, TechAgent, BillingAgent, ComplaintAgent, GeneralAgent, ResponseAgent
from session_manager import EnhancedSessionManager, default_session_manager
from core.message_bus import MessageBus
from core.shared_blackboard import SharedBlackboard
from core.monitoring import MetricsCollector, CircuitBreaker, SLAAlertManager, OpenAICompatibleClient
from cache.response_cache import ResponseCache
from router.query_router import QueryRouter, RoutingResult
from collaboration.orchestrator import CollaborationOrchestrator
from logger import get_logger

logger = get_logger("graph")


def _format_duration(seconds: float) -> str:
    """格式化耗时显示"""
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    return f"{seconds:.1f}s"


# ===== 状态定义 =====
class AgentState(TypedDict):
    session_id: str
    current_agent: str
    customer_query: str
    query_type: str
    response: str
    complexity: int
    fast_path: bool
    collaboration_mode: str
    cached: bool
    agents_used: List[str]
    resolution_status: str  # resolved | uncertain | failed | escalated


# ===== 全局实例 =====
llm = None
agents_dict = {}
session_mgr = default_session_manager
bus = MessageBus()
bb = SharedBlackboard()
cache = ResponseCache(l1_max=CACHE_L1_MAX, l2_max=CACHE_L2_MAX, default_ttl=CACHE_TTL)
circuit_breaker = CircuitBreaker()
sla_alert_mgr = SLAAlertManager(bus=bus)

# 可选 Redis 持久化（Session + Cache）
if SESSION_STORAGE_BACKEND == "redis":
    try:
        session_mgr = EnhancedSessionManager(
            storage_backend="redis",
            window_size=SESSION_WINDOW_SIZE,
            url=REDIS_URL,
        )
        cache._init_redis()
    except Exception:
        logger.warning("Redis 初始化失败，回退到内存模式")

router = None
erp = None
response_agent = None
orchestrator = CollaborationOrchestrator(bus, bb)
metrics = MetricsCollector()


def initialize_llm():
    global llm
    if llm is None:
        llm = OpenAICompatibleClient(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            model=OPENAI_MODEL,
            circuit_breaker=circuit_breaker,
        )
    return llm


def initialize_agents():
    global agents_dict, response_agent, erp
    if not agents_dict:
        _llm = initialize_llm()
        if erp is None:
            from erp.factory import create_erp_adapter
            erp = create_erp_adapter()
        agent_classes = {
            "product_agent": ProductAgent,
            "tech_agent": TechAgent,
            "billing_agent": BillingAgent,
            "complaint_agent": ComplaintAgent,
            "general_agent": GeneralAgent,
        }
        for name, cls in agent_classes.items():
            agent = cls()
            agent.set_llm(_llm)
            agent.set_session_manager(session_mgr)
            agent.set_bus(bus)
            agent.set_blackboard(bb)
            agent.set_erp(erp)
            agents_dict[name] = agent

        # ResponseAgent: Router → Expert → Response 三层架构的最终环节
        response_agent = ResponseAgent(
            session_manager=session_mgr,
            message_bus=bus,
            blackboard=bb,
            cache=cache,
        )
        response_agent.set_llm(_llm)

        logger.info(f"初始化 {len(agents_dict)} 个专家 Agent + ResponseAgent 完成 (ERP_MODE={ERP_MODE})")
    return agents_dict


def initialize_router():
    global router
    if router is None:
        router = QueryRouter(llm=initialize_llm(), complexity_threshold=ROUTING_COMPLEXITY_THRESHOLD)
    return router


# ===== 图节点（v3.0 全部原生异步）=====

async def classify_query_node(state: AgentState) -> AgentState:
    """双层路由节点：LLM Router + Rule Classifier + 复杂度评分（v3.2: 熔断器降级）
    v3.3: 移除重复 add_message 调用（由各 Agent 的 _process_with_llm 统一写入）
    """
    query = state["customer_query"]
    session_id = state.get("session_id", "default")

    session_mgr.create_session(session_id)
    # v3.3: 不在此处写入用户消息，避免与 _process_with_llm 重复

    _router = initialize_router()
    context = session_mgr.get_conversation_context(session_id)
    context_text = "\n".join([m.get("content", "") for m in context[-6:]]) if context else ""

    # v3.2: 熔断器检查 — OPEN 状态时跳过 LLM，仅用规则分类
    if not circuit_breaker.should_allow():
        logger.warning("[Router] LLM 熔断中，降级为纯规则分类")
        rule_type = _router._rule_classify(query)
        from router.query_router import INTENT_AGENT_MAP
        final_type = rule_type or "general_inquiry"
        complexity = _router._score_complexity(query, final_type, context_text)
        result = RoutingResult(
            query_type=final_type,
            agent_name=INTENT_AGENT_MAP.get(final_type, "general_agent"),
            complexity=complexity,
            fast_path=complexity < _router.complexity_threshold,
            confidence=0.3,
            raw_llm_result="[circuit_breaker_open]",
            rule_override=True,
        )
    else:
        try:
            result = await _router.route(query, context_text)
        except Exception as e:
            logger.error(f"router error: {e}")
            result = RoutingResult()

    state["query_type"] = result.query_type
    state["current_agent"] = result.agent_name
    state["complexity"] = result.complexity
    state["fast_path"] = result.fast_path
    state["collaboration_mode"] = ""

    logger.info(f"[Router] type={result.query_type} agent={result.agent_name} "
                f"complexity={result.complexity} fast_path={result.fast_path}")

    # 写入黑板供下游使用
    await bb.write("last_routing", {
        "query_type": result.query_type,
        "agent": result.agent_name,
        "complexity": result.complexity,
    })

    return state


async def check_cache_node(state: AgentState) -> AgentState:
    """缓存检查节点"""
    query = state["customer_query"]
    cached = cache.get(query)
    if cached:
        state["response"] = cached
        state["cached"] = True
        state["current_agent"] = "cache"
        state["collaboration_mode"] = "cache_hit"
        logger.info(f"[Cache] HIT: {query[:30]}...")
    else:
        state["cached"] = False
        logger.debug(f"[Cache] MISS: {query[:30]}...")
    return state


# ---- 统一协作模式执行函数 ----

async def execute_collaboration(state: AgentState, mode_name: str) -> AgentState:
    """统一执行协作模式"""
    _ = initialize_agents()

    start = time.time()
    try:
        routing_result = RoutingResult(
            query_type=state.get("query_type", "general_inquiry"),
            agent_name=state.get("current_agent", "general_agent"),
            complexity=state.get("complexity", 0),
            fast_path=state.get("fast_path", True),
        )
        _, context = orchestrator.build_context(routing_result, state)
        mode = orchestrator._modes.get(mode_name)
        if not mode:
            mode = orchestrator._modes["sequential"]
            mode_name = "sequential"
        result = await mode.execute(agents_dict, dict(state), context)
    except Exception as e:
        logger.error(f"[{mode_name}] error: {e}")
        result = {"response": "处理出错，请重试", "mode": mode_name, "agents_used": []}

    elapsed = time.time() - start
    state["response"] = result.get("response", "")
    state["collaboration_mode"] = result.get("mode", mode_name)
    state["agents_used"] = result.get("agents_used", [])
    logger.info(f"[{mode_name}] agents={state['agents_used']} {_format_duration(elapsed)}")
    return state


def _make_collaboration_node(mode_name: str):
    """协作模式节点工厂（消除 4 个同构函数的重复）"""
    async def _node(state: AgentState) -> AgentState:
        return await execute_collaboration(state, mode_name)
    _node.__doc__ = f"{mode_name} 协作模式节点"
    _node.__name__ = f"{mode_name}_node"
    return _node


sequential_node = _make_collaboration_node("sequential")
parallel_node = _make_collaboration_node("parallel")
consultation_node = _make_collaboration_node("consultation")
hierarchical_node = _make_collaboration_node("hierarchical")


async def final_response_node(state: AgentState) -> AgentState:
    """最终响应节点（v3.0: 委托 ResponseAgent 处理后处理逻辑）"""
    _ = initialize_agents()
    global response_agent
    if response_agent:
        try:
            state = await response_agent.process(dict(state))
        except Exception as e:
            logger.error(f"[ResponseAgent] error: {e}, 降级到基础处理")
            _fallback_post_process(state)
    else:
        logger.warning("[ResponseAgent] 未初始化，使用基础后处理")
        _fallback_post_process(state)

    return state


def _fallback_post_process(state: AgentState):
    """降级后处理：仅做缓存和会话写入（不替代 ResponseAgent 完整功能）"""
    if state.get("response") and not state.get("cached", False):
        cache.put(state["customer_query"], state["response"])
    try:
        session_mgr.add_message(state.get("session_id", ""), state["response"], is_user=False)
    except Exception:
        pass


# ===== 条件路由函数 =====

def select_collaboration_mode(state: AgentState) -> str:
    """LangGraph Conditional Edge：委托 orchestrator 统一选择
    v3.3: 移除 initialize_agents() 调用，模式选择不依赖 Agent 实例"""
    routing_result = RoutingResult(
        query_type=state.get("query_type", "general_inquiry"),
        agent_name=state.get("current_agent", "general_agent"),
        complexity=state.get("complexity", 0),
        fast_path=state.get("fast_path", True),
    )
    mode_name = orchestrator.select_mode_name(routing_result, state)
    logger.info(f"[ModeSelect] → {mode_name}")
    return mode_name


# ===== 构建图 =====

def make_graph():
    """构建 LangGraph 工作流图（v3.3 性能优化版）
    三层状态机架构：
      Layer 0: Cache Check（check_cache）— 缓存命中直接跳到 final_response
      Layer 1: Router（classify_query_node）— 双层意图识别 + 复杂度评分
      Layer 2: Expert Agent（4 种协作模式节点）— 动态路由选择
      Layer 3: ResponseAgent（final_response_node）— 缓存/会话/SLA/事件

    v3.3 优化：缓存检查前置，命中时跳过 LLM 路由调用（节省 2-8s）
    """
    workflow = StateGraph(AgentState)

    # 添加节点
    workflow.add_node("classify_query", classify_query_node)
    workflow.add_node("check_cache", check_cache_node)
    workflow.add_node("sequential", sequential_node)
    workflow.add_node("parallel", parallel_node)
    workflow.add_node("consultation", consultation_node)
    workflow.add_node("hierarchical", hierarchical_node)
    workflow.add_node("final_response", final_response_node)

    # v3.3: 入口改为缓存检查（命中直接跳到 final_response，跳过 LLM 路由）
    workflow.set_entry_point("check_cache")

    # cache → 条件分支：命中直接返回，未命中进入路由
    workflow.add_conditional_edges(
        "check_cache",
        lambda s: "final_response" if s.get("cached", False) else "classify_query",
        {
            "final_response": "final_response",
            "classify_query": "classify_query",
        }
    )

    # classify → 条件路由选择协作模式
    workflow.add_conditional_edges(
        "classify_query",
        select_collaboration_mode,
        {
            "sequential": "sequential",
            "parallel": "parallel",
            "consultation": "consultation",
            "hierarchical": "hierarchical",
        }
    )

    # 所有协作模式 → final_response
    workflow.add_edge("sequential", "final_response")
    workflow.add_edge("parallel", "final_response")
    workflow.add_edge("consultation", "final_response")
    workflow.add_edge("hierarchical", "final_response")

    # 结束
    workflow.set_finish_point("final_response")

    app = workflow.compile()
    logger.info("[Graph] LangGraph v3.3 构建完成 - 缓存前置 + 原生异步 + 统一编排 + 指标采集")
    return app


if __name__ == "__main__":
    app = make_graph()
    logger.info("多智能体客服系统 v3.0 启动成功")
