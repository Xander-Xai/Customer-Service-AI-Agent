"""
药妆智多星多智能体客服系统（v4.1 稳定版）
LangGraph 状态机 + 双层路由 + 5种协作模式 + 二级缓存 + 通信总线 + RAG + 工具调用

v3.0 核心改造：
- 全部图节点原生异步，消除 asyncio.to_thread 死锁风险
- 模式选择逻辑统一委托 orchestrator（消除重复代码）
- 结构化日志替换 print
- 性能指标实时采集

v3.5 新增：
- RAG 知识库（ChromaDB）：产品成分/FAQ/技术支持检索增强
- Function Calling：Agent 可自主调用 ERP 工具
- ReAct 推理模式：第 5 种协作模式，适用于复杂多步骤查询

v3.8 稳定化：
- 前端 WebSocket 修复 + 暗色主题
- 安全加固：限流/认证/输入验证/注入防护/安全头
- 并发安全：asyncio.Lock 初始化保护
- 代码瘦身：消除重复代码，统一模板方法
- 响应清洗：移除 LLM 响应中的调试代码

v4.1 依赖注入：
- 新增 ServiceContainer（core/container.py）集中管理组件生命周期
- 新增 build_graph(container) 作为推荐的图构建方式
- 保留 make_graph() 向后兼容（内部创建容器或使用全局变量）
"""
import asyncio
import time
from typing import List, TypedDict
from langgraph.graph import StateGraph, END  # END: LangGraph 终止节点

from config import (
    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL,
    ROUTING_COMPLEXITY_THRESHOLD, CACHE_L1_MAX, CACHE_L2_MAX, CACHE_TTL,
    SESSION_STORAGE_BACKEND, SESSION_WINDOW_SIZE, REDIS_URL, ERP_MODE,
    DEV_MODE,
)

from agents import ProductAgent, TechAgent, BillingAgent, ComplaintAgent, GeneralAgent, ResponseAgent, ReActAgent
from session_manager import EnhancedSessionManager, default_session_manager
from core.container import ServiceContainer
from core.message_bus import MessageBus
from core.shared_blackboard import SharedBlackboard
from core.monitoring import MetricsCollector, CircuitBreaker, SLAAlertManager, OpenAICompatibleClient
from cache.response_cache import ResponseCache
from router.query_router import QueryRouter, RoutingResult
from collaboration.orchestrator import CollaborationOrchestrator
from logger import get_logger, set_trace_id

# v4.1: 规则引擎降级（开发模式）
try:
    from llm.rule_based_llm import RuleBasedLLM
except ImportError:
    RuleBasedLLM = None

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

# v3.5: RAG 知识库 + 工具注册
knowledge_base = None
tool_registry = None
_init_lock = asyncio.Lock()


async def _initialize_llm_internal():
    """内部调用，不加锁（由 initialize_agents 或 initialize_router 保护）
    v4.1: 智能降级 - API Key 无效时自动切换到规则引擎（仅开发模式）
    """
    global llm
    if llm is None:
        # v4.1: 检查 API Key 是否有效
        api_key_valid = OPENAI_API_KEY and not OPENAI_API_KEY.startswith("your_")

        if not api_key_valid and DEV_MODE:
            # 开发模式：API Key 无效时自动降级到规则引擎
            if RuleBasedLLM:
                logger.warning("⚠️ DeepSeek API Key 未配置，自动切换到规则引擎模式（开发降级）")
                logger.warning("💡 配置真实的 API Key：编辑 .env.dev 文件第 7 行")
                llm = RuleBasedLLM()
            else:
                logger.error("❌ 规则引擎模块不可用，请配置 API Key")
                llm = OpenAICompatibleClient(
                    api_key=OPENAI_API_KEY or "invalid",
                    base_url=OPENAI_BASE_URL,
                    model=OPENAI_MODEL,
                    circuit_breaker=circuit_breaker,
                )
        else:
            # 生产模式或 API Key 有效：使用真实 LLM
            llm = OpenAICompatibleClient(
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_BASE_URL,
                model=OPENAI_MODEL,
                circuit_breaker=circuit_breaker,
            )
    return llm


async def initialize_llm():
    global llm
    async with _init_lock:
        return await _initialize_llm_internal()


async def initialize_agents():
    global agents_dict, response_agent, erp
    async with _init_lock:
        if not agents_dict:
            _llm = await _initialize_llm_internal()
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

            # v3.5: 注入 RAG 知识库到需要检索的 Agent（直接初始化，避免嵌套锁死锁）
            await _init_rag_and_tools_internal()
            for name in ("product_agent", "tech_agent", "complaint_agent"):
                if name in agents_dict:
                    agents_dict[name].set_knowledge_base(knowledge_base)

            # v3.5: 创建 ReAct 推理 Agent
            react_agent = ReActAgent()
            react_agent.set_llm(_llm)
            react_agent.set_session_manager(session_mgr)
            react_agent.set_bus(bus)
            react_agent.set_blackboard(bb)
            react_agent.set_erp(erp)
            react_agent.set_knowledge_base(knowledge_base)
            react_agent.set_tool_registry(tool_registry)
            agents_dict["react_agent"] = react_agent

            # ResponseAgent: Router → Expert → Response 三层架构的最终环节
            response_agent = ResponseAgent(
                session_manager=session_mgr,
                message_bus=bus,
                blackboard=bb,
                cache=cache,
            )
            response_agent.set_llm(_llm)

            logger.info(f"初始化 {len(agents_dict)} 个 Agent（含 ReActAgent）完成 (ERP_MODE={ERP_MODE})")
    return agents_dict


async def initialize_router():
    global router
    async with _init_lock:
        if router is None:
            router = QueryRouter(llm=await _initialize_llm_internal(), complexity_threshold=ROUTING_COMPLEXITY_THRESHOLD)
        return router


async def _init_rag_and_tools_internal():
    """v3.5: 初始化 RAG 知识库 + 工具注册（内部调用，不加锁，由 initialize_agents 保护）"""
    global knowledge_base, tool_registry, erp
    if knowledge_base is None:
        from rag.knowledge_base import CosmeticsKnowledgeBase
        from rag.seed_data import seed_product_knowledge, seed_faq, seed_tech_support, seed_complaint_knowledge, seed_supplementary_data
        knowledge_base = CosmeticsKnowledgeBase()
        seed_product_knowledge(knowledge_base)
        seed_faq(knowledge_base)
        seed_tech_support(knowledge_base)
        seed_complaint_knowledge(knowledge_base)
        seed_supplementary_data(knowledge_base)
        logger.info(f"RAG 知识库初始化完成 (product={knowledge_base.get_collection_count('product_knowledge')}, "
                    f"faq={knowledge_base.get_collection_count('faq')}, "
                    f"tech={knowledge_base.get_collection_count('tech_support')}, "
                    f"complaint={knowledge_base.get_collection_count('complaint_knowledge')})")
    if tool_registry is None:
        from tools.erp_tools import create_erp_tools
        if erp is None:
            from erp.factory import create_erp_adapter
            erp = create_erp_adapter()
        tool_registry = create_erp_tools(erp)
        logger.info(f"工具注册完成: {tool_registry.list_tools()}")


# ===== 图节点（v3.0 全部原生异步）=====

async def classify_query_node(state: AgentState) -> AgentState:
    """双层路由节点：LLM Router + Rule Classifier + 复杂度评分（v3.2: 熔断器降级）
    v3.3: 移除重复 add_message 调用（由各 Agent 的 _process_with_llm 统一写入）
    v4.1: 从 state 传播 trace_id 到 contextvars
    """
    # 分布式追踪：从 state 传播 trace_id
    trace_id = state.get("trace_id", "")
    if trace_id:
        set_trace_id(trace_id)

    query = state["customer_query"]
    session_id = state.get("session_id", "default")

    await session_mgr.create_session(session_id)
    # v3.3: 不在此处写入用户消息，避免与 _process_with_llm 重复

    _router = await initialize_router()
    context = await session_mgr.get_conversation_context(session_id)
    context_text = "\n".join([m.get("content", "") for m in context[-6:]]) if context else ""

    # v3.2: 熔断器检查 — OPEN 状态时跳过 LLM，仅用规则分类
    if not await circuit_breaker.should_allow():
        logger.warning("[Router] LLM 熔断中，降级为纯规则分类")
        from router.query_router import INTENT_AGENT_MAP
        rule_type, _, complexity = _router._rule_classify_and_score(query, context_text)
        final_type = rule_type or "general_inquiry"
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
            logger.warning(f"[Router] 路由异常，降级到通用查询: {e}")
            result = RoutingResult(
                query_type="general_inquiry",
                agent_name="general_agent",
                complexity=30,
                fast_path=True,
                confidence=0.0,
                raw_llm_result="[error_fallback]",
                rule_override=True,
            )

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
    _ = await initialize_agents()

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
    """最终响应节点（v4.3: 集成质量评估 + 自动模式升级重试）
    三层架构的最终环节：缓存写入、会话记录、SLA 监控、事件广播。
    v4.3: 当质量评分极低时，自动升级到更复杂模式重新处理。"""
    _ = await initialize_agents()
    global response_agent
    if response_agent:
        try:
            state = await response_agent.process(dict(state))
        except Exception as e:
            logger.error(f"[ResponseAgent] error: {e}, 降级到基础处理")
            _fallback_post_process(state)

        # v4.3: 质量评估触发模式升级重试
        if state.get("_needs_upgrade") and not state.get("_retried_failed"):
            current_mode = state.get("collaboration_mode", "sequential")
            new_mode, upgrade_ctx = orchestrator.upgrade_mode(current_mode, state)
            if new_mode != current_mode:
                logger.info(f"[ModeUpgrade] 执行升级重试: {current_mode} → {new_mode}")
                state["_retried_failed"] = True  # 防止无限循环
                state["collaboration_mode"] = new_mode
                try:
                    mode = orchestrator._modes.get(new_mode)
                    if mode:
                        result = await mode.execute(agents_dict, dict(state), upgrade_ctx)
                        state["response"] = result.get("response", state.get("response", ""))
                        state["agents_used"] = result.get("agents_used", [])
                        state["collaboration_mode"] = result.get("mode", new_mode)
                        # 重新评估质量
                        state = await response_agent.process(dict(state))
                        logger.info(f"[ModeUpgrade] 升级重试完成: mode={new_mode}")
                except Exception as e:
                    logger.error(f"[ModeUpgrade] 升级重试失败: {e}，保留原响应")
    else:
        logger.warning("[ResponseAgent] 未初始化，使用基础后处理")
        _fallback_post_process(state)

    return state


def _fallback_post_process(state: AgentState):
    """降级后处理：仅做缓存写入（不重复写入会话记录，避免与 ResponseAgent 双写）
    v3.4: 移除 session_mgr.add_message 调用，因为 ResponseAgent 已在 process() 中写入，
    即使 ResponseAgent 部分执行后失败，session 写入在缓存写入之前已完成。"""
    if state.get("response") and not state.get("cached", False):
        cache.put(state["customer_query"], state["response"])


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
    """构建 LangGraph 工作流图（v3.5 RAG + Function Calling + ReAct 版）
    三层状态机架构：
      Layer 0: Cache Check（check_cache）— 缓存命中直接跳到 final_response
      Layer 1: Router（classify_query_node）— 双层意图识别 + 复杂度评分
      Layer 2: Expert Agent（5 种协作模式节点）— 动态路由选择（含 ReAct 推理）
      Layer 3: ResponseAgent（final_response_node）— 缓存/会话/SLA/事件

    v3.3 优化：缓存检查前置，命中时跳过 LLM 路由调用（节省 2-8s）
    v3.5 新增：react 节点支持 RAG + Function Calling 自主推理
    """
    workflow = StateGraph(AgentState)

    # 添加节点
    workflow.add_node("classify_query", classify_query_node)
    workflow.add_node("check_cache", check_cache_node)
    workflow.add_node("sequential", sequential_node)
    workflow.add_node("parallel", parallel_node)
    workflow.add_node("consultation", consultation_node)
    workflow.add_node("hierarchical", hierarchical_node)
    workflow.add_node("react", _make_collaboration_node("react"))
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

    # classify → 条件路由选择协作模式（v3.5: 含 react 模式）
    workflow.add_conditional_edges(
        "classify_query",
        select_collaboration_mode,
        {
            "sequential": "sequential",
            "parallel": "parallel",
            "consultation": "consultation",
            "hierarchical": "hierarchical",
            "react": "react",
        }
    )

    # 所有协作模式 → final_response
    workflow.add_edge("sequential", "final_response")
    workflow.add_edge("parallel", "final_response")
    workflow.add_edge("consultation", "final_response")
    workflow.add_edge("hierarchical", "final_response")
    workflow.add_edge("react", "final_response")

    # 结束
    workflow.set_finish_point("final_response")

    app = workflow.compile()
    logger.info("[Graph] LangGraph v3.6 构建完成 - 缓存前置 + 原生异步 + 5种协作模式 + RAG + 工具调用")
    return app


# ===== v4.1: 基于容器的图构建（推荐方式）=====


def build_graph(container: ServiceContainer):
    """通过 ServiceContainer 构建 LangGraph 工作流图（v4.1 推荐方式）

    与 make_graph() 功能完全相同，但通过容器注入依赖而非使用全局变量。
    所有图节点函数通过闭包绑定到容器实例，避免隐式全局状态。

    Usage:
        container = ServiceContainer()
        await container.initialize()
        app = build_graph(container)
    """
    c = container  # 短别名，闭包捕获

    # ---- 容器绑定的图节点 ----

    async def _classify_query_node(state: AgentState) -> AgentState:
        """双层路由节点（容器版本）"""
        # 分布式追踪：从 state 传播 trace_id
        trace_id = state.get("trace_id", "")
        if trace_id:
            set_trace_id(trace_id)

        query = state["customer_query"]
        session_id = state.get("session_id", "default")

        c.session_mgr.create_session(session_id)

        # 确保 router 已初始化
        if c.router is None:
            await c._init_router()

        context = await c.session_mgr.get_conversation_context(session_id)
        context_text = "\n".join(
            [m.get("content", "") for m in context[-6:]]
        ) if context else ""

        if not await c.circuit_breaker.should_allow():
            logger.warning("[Router] LLM 熔断中，降级为纯规则分类")
            from router.query_router import INTENT_AGENT_MAP
            rule_type, _, complexity = c.router._rule_classify_and_score(
                query, context_text,
            )
            final_type = rule_type or "general_inquiry"
            result = RoutingResult(
                query_type=final_type,
                agent_name=INTENT_AGENT_MAP.get(final_type, "general_agent"),
                complexity=complexity,
                fast_path=complexity < c.router.complexity_threshold,
                confidence=0.3,
                raw_llm_result="[circuit_breaker_open]",
                rule_override=True,
            )
        else:
            try:
                result = await c.router.route(query, context_text)
            except Exception as e:
                logger.warning(f"[Router] 路由异常，降级到通用查询: {e}")
                result = RoutingResult(
                    query_type="general_inquiry",
                    agent_name="general_agent",
                    complexity=30,
                    fast_path=True,
                    confidence=0.0,
                    raw_llm_result="[error_fallback]",
                    rule_override=True,
                )

        state["query_type"] = result.query_type
        state["current_agent"] = result.agent_name
        state["complexity"] = result.complexity
        state["fast_path"] = result.fast_path
        state["collaboration_mode"] = ""

        logger.info(
            f"[Router] type={result.query_type} agent={result.agent_name} "
            f"complexity={result.complexity} fast_path={result.fast_path}"
        )

        await c.bb.write("last_routing", {
            "query_type": result.query_type,
            "agent": result.agent_name,
            "complexity": result.complexity,
        })

        return state

    async def _check_cache_node(state: AgentState) -> AgentState:
        """缓存检查节点（容器版本）"""
        query = state["customer_query"]
        cached = c.cache.get(query)
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

    async def _execute_collaboration(
        state: AgentState, mode_name: str,
    ) -> AgentState:
        """统一执行协作模式（容器版本）"""
        # 确保 agents 已初始化
        if not c.agents_dict:
            await c._init_agents()

        start = time.time()
        try:
            routing_result = RoutingResult(
                query_type=state.get("query_type", "general_inquiry"),
                agent_name=state.get("current_agent", "general_agent"),
                complexity=state.get("complexity", 0),
                fast_path=state.get("fast_path", True),
            )
            _, ctx = c.orchestrator.build_context(routing_result, state)
            mode = c.orchestrator._modes.get(mode_name)
            if not mode:
                mode = c.orchestrator._modes["sequential"]
                mode_name = "sequential"
            result = await mode.execute(c.agents_dict, dict(state), ctx)
        except Exception as e:
            logger.error(f"[{mode_name}] error: {e}")
            result = {
                "response": "处理出错，请重试",
                "mode": mode_name,
                "agents_used": [],
            }

        elapsed = time.time() - start
        state["response"] = result.get("response", "")
        state["collaboration_mode"] = result.get("mode", mode_name)
        state["agents_used"] = result.get("agents_used", [])
        logger.info(
            f"[{mode_name}] agents={state['agents_used']} "
            f"{_format_duration(elapsed)}"
        )
        return state

    def _make_container_collaboration_node(mode_name: str):
        """协作模式节点工厂（容器版本）"""
        async def _node(state: AgentState) -> AgentState:
            return await _execute_collaboration(state, mode_name)
        _node.__doc__ = f"{mode_name} 协作模式节点（容器版）"
        _node.__name__ = f"container_{mode_name}_node"
        return _node

    async def _final_response_node(state: AgentState) -> AgentState:
        """最终响应节点（容器版本）"""
        if not c.agents_dict:
            await c._init_agents()

        if c.response_agent:
            try:
                state = await c.response_agent.process(dict(state))
            except Exception as e:
                logger.error(
                    f"[ResponseAgent] error: {e}, 降级到基础处理"
                )
                _fallback_post_process_container(state)
        else:
            logger.warning("[ResponseAgent] 未初始化，使用基础后处理")
            _fallback_post_process_container(state)

        return state

    def _fallback_post_process_container(state: AgentState):
        """降级后处理（容器版本）"""
        if state.get("response") and not state.get("cached", False):
            c.cache.put(state["customer_query"], state["response"])

    def _select_collaboration_mode(state: AgentState) -> str:
        """条件路由（容器版本）"""
        routing_result = RoutingResult(
            query_type=state.get("query_type", "general_inquiry"),
            agent_name=state.get("current_agent", "general_agent"),
            complexity=state.get("complexity", 0),
            fast_path=state.get("fast_path", True),
        )
        mode_name = c.orchestrator.select_mode_name(routing_result, state)
        logger.info(f"[ModeSelect] -> {mode_name}")
        return mode_name

    # ---- 构建图 ----
    workflow = StateGraph(AgentState)

    workflow.add_node("classify_query", _classify_query_node)
    workflow.add_node("check_cache", _check_cache_node)
    workflow.add_node("sequential", _make_container_collaboration_node("sequential"))
    workflow.add_node("parallel", _make_container_collaboration_node("parallel"))
    workflow.add_node("consultation", _make_container_collaboration_node("consultation"))
    workflow.add_node("hierarchical", _make_container_collaboration_node("hierarchical"))
    workflow.add_node("react", _make_container_collaboration_node("react"))
    workflow.add_node("final_response", _final_response_node)

    workflow.set_entry_point("check_cache")

    workflow.add_conditional_edges(
        "check_cache",
        lambda s: "final_response" if s.get("cached", False) else "classify_query",
        {
            "final_response": "final_response",
            "classify_query": "classify_query",
        },
    )

    workflow.add_conditional_edges(
        "classify_query",
        _select_collaboration_mode,
        {
            "sequential": "sequential",
            "parallel": "parallel",
            "consultation": "consultation",
            "hierarchical": "hierarchical",
            "react": "react",
        },
    )

    workflow.add_edge("sequential", "final_response")
    workflow.add_edge("parallel", "final_response")
    workflow.add_edge("consultation", "final_response")
    workflow.add_edge("hierarchical", "final_response")
    workflow.add_edge("react", "final_response")

    workflow.set_finish_point("final_response")

    app = workflow.compile()
    logger.info("[Graph] LangGraph v4.1 (容器版) 构建完成")
    return app


if __name__ == "__main__":
    app = make_graph()
    logger.info("药妆智多星多智能体客服系统 v3.8 启动成功")
