"""
药妆智多星多智能体客服系统（v4.5 统一版）
LangGraph 状态机 + 双层路由 + 5种协作模式 + 二级缓存 + 通信总线 + RAG + 工具调用

v4.5 统一图构建：
- 消除 3 处图拓扑重复定义（multi_agent_customer_service / build_graph / container._build_graph）
- build_graph(container) 作为唯一图构建入口
- make_graph() 保留为向后兼容包装器（内部创建 ServiceContainer）
- 移除模块级全局变量，所有依赖通过容器注入

v4.3 运行时模式升级：
- sequential -> consultation -> parallel -> react 自动升级重试
- 质量评分极低时触发更复杂模式重新处理

v4.1 依赖注入：
- ServiceContainer（core/container.py）集中管理组件生命周期
"""

import time

from langgraph.graph import StateGraph

from core.container import ServiceContainer
from core.state import AgentState
from logger import get_logger, set_trace_id
from router.query_router import RoutingResult

logger = get_logger("graph")


def _format_duration(seconds: float) -> str:
    """格式化耗时显示"""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


# ===== v4.5: 唯一图构建入口 =====


def build_graph(container: ServiceContainer):
    """通过 ServiceContainer 构建 LangGraph 工作流图（唯一图构建入口）

    三层状态机架构：
      Layer 0: Cache Check（check_cache）— 缓存命中直接跳到 final_response
      Layer 1: Router（classify_query_node）— 双层意图识别 + 复杂度评分
      Layer 2: Expert Agent（5 种协作模式节点）— 动态路由选择（含 ReAct 推理）
      Layer 3: ResponseAgent（final_response_node）— 缓存/会话/SLA/事件

    v4.3: 运行时模式升级（sequential -> consultation -> parallel -> react）

    Usage:
        container = ServiceContainer()
        await container.initialize()
        app = build_graph(container)
    """
    c = container  # 短别名，闭包捕获

    # ---- 容器绑定的图节点 ----

    async def _classify_query_node(state: AgentState) -> AgentState:
        """双层路由节点：LLM Router + Rule Classifier + 复杂度评分（v3.2: 熔断器降级）
        v4.1: 从 state 传播 trace_id 到 contextvars
        """
        trace_id = state.get("trace_id", "")
        if trace_id:
            set_trace_id(trace_id)

        query = state["customer_query"]
        session_id = state.get("session_id", "default")

        await c.session_mgr.create_session(session_id)

        # 确保 router 已初始化
        if c.router is None:
            await c._init_router()

        context = await c.session_mgr.get_conversation_context(session_id)
        context_text = "\n".join([m.get("content", "") for m in context[-6:]]) if context else ""

        # v3.2: 熔断器检查 — OPEN 状态时跳过 LLM，仅用规则分类
        if not await c.circuit_breaker.should_allow():
            logger.warning("[Router] LLM 熔断中，降级为纯规则分类")
            from router.query_router import INTENT_AGENT_MAP

            rule_type, _, complexity = c.router._rule_classify_and_score(
                query,
                context_text,
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

        # 写入黑板供下游使用
        await c.bb.write(
            "last_routing",
            {
                "query_type": result.query_type,
                "agent": result.agent_name,
                "complexity": result.complexity,
            },
        )

        return state

    async def _check_cache_node(state: AgentState) -> AgentState:
        """缓存检查节点"""
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
        state: AgentState,
        mode_name: str,
    ) -> AgentState:
        """统一执行协作模式"""
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
        logger.info(f"[{mode_name}] agents={state['agents_used']} {_format_duration(elapsed)}")
        return state

    def _make_collaboration_node(mode_name: str):
        """协作模式节点工厂"""

        async def _node(state: AgentState) -> AgentState:
            return await _execute_collaboration(state, mode_name)

        _node.__doc__ = f"{mode_name} 协作模式节点"
        _node.__name__ = f"{mode_name}_node"
        return _node

    async def _final_response_node(state: AgentState) -> AgentState:
        """最终响应节点（v4.3: 集成质量评估 + 自动模式升级重试）
        三层架构的最终环节：缓存写入、会话记录、SLA 监控、事件广播。
        v4.3: 当质量评分极低时，自动升级到更复杂模式重新处理。"""
        if not c.agents_dict:
            await c._init_agents()

        if c.response_agent:
            try:
                state = await c.response_agent.process(dict(state))
            except Exception as e:
                logger.error(f"[ResponseAgent] error: {e}, 降级到基础处理")
                _fallback_post_process(state)

            # v4.3: 质量评估触发模式升级重试
            if state.get("_needs_upgrade") and not state.get("_retried_failed"):
                current_mode = state.get("collaboration_mode", "sequential")
                new_mode, upgrade_ctx = c.orchestrator.upgrade_mode(current_mode, state)
                if new_mode != current_mode:
                    logger.info(f"[ModeUpgrade] 执行升级重试: {current_mode} -> {new_mode}")
                    state["_retried_failed"] = True  # 防止无限循环
                    state["collaboration_mode"] = new_mode
                    try:
                        mode = c.orchestrator._modes.get(new_mode)
                        if mode:
                            result = await mode.execute(c.agents_dict, dict(state), upgrade_ctx)
                            state["response"] = result.get("response", state.get("response", ""))
                            state["agents_used"] = result.get("agents_used", [])
                            state["collaboration_mode"] = result.get("mode", new_mode)
                            # v5.0: 升级重试后只更新缓存，不重复完整后处理（避免重复写入会话/SLA/事件）
                            if state.get("response") and not state.get("cached", False) and c.cache:
                                c.cache.put(state["customer_query"], state["response"])
                            logger.info(f"[ModeUpgrade] 升级重试完成: mode={new_mode}")
                    except Exception as e:
                        logger.error(f"[ModeUpgrade] 升级重试失败: {e}，保留原响应")
        else:
            logger.warning("[ResponseAgent] 未初始化，使用基础后处理")
            _fallback_post_process(state)

        return state

    def _fallback_post_process(state: AgentState):
        """降级后处理：仅做缓存写入"""
        if state.get("response") and not state.get("cached", False):
            c.cache.put(state["customer_query"], state["response"])

    def _select_collaboration_mode(state: AgentState) -> str:
        """LangGraph Conditional Edge：委托 orchestrator 统一选择"""
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

    # 添加节点
    workflow.add_node("classify_query", _classify_query_node)
    workflow.add_node("check_cache", _check_cache_node)
    workflow.add_node("sequential", _make_collaboration_node("sequential"))
    workflow.add_node("parallel", _make_collaboration_node("parallel"))
    workflow.add_node("consultation", _make_collaboration_node("consultation"))
    workflow.add_node("hierarchical", _make_collaboration_node("hierarchical"))
    workflow.add_node("react", _make_collaboration_node("react"))
    workflow.add_node("final_response", _final_response_node)

    # 入口：缓存检查（命中直接跳到 final_response，跳过 LLM 路由）
    workflow.set_entry_point("check_cache")

    # cache -> 条件分支：命中直接返回，未命中进入路由
    workflow.add_conditional_edges(
        "check_cache",
        lambda s: "final_response" if s.get("cached", False) else "classify_query",
        {
            "final_response": "final_response",
            "classify_query": "classify_query",
        },
    )

    # classify -> 条件路由选择协作模式（含 react 模式）
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

    # 所有协作模式 -> final_response
    workflow.add_edge("sequential", "final_response")
    workflow.add_edge("parallel", "final_response")
    workflow.add_edge("consultation", "final_response")
    workflow.add_edge("hierarchical", "final_response")
    workflow.add_edge("react", "final_response")

    # 结束
    workflow.set_finish_point("final_response")

    app = workflow.compile()
    logger.info("[Graph] LangGraph v4.5 构建完成 - 缓存前置 + 5种协作模式 + RAG + 模式升级")
    return app


# ===== 向后兼容：make_graph() 包装器 =====


def make_graph():
    """向后兼容包装器：内部创建 ServiceContainer 并调用 build_graph()。

    已弃用 —— 新代码应直接使用:
        container = ServiceContainer()
        await container.initialize()
        app = build_graph(container)

    注意：此函数会创建一个新的 ServiceContainer 实例，
    仅包含同步初始化的基础设施组件（不包含 LLM/Agents/Router 等异步组件）。
    图节点会在首次调用时懒初始化所需组件。
    """
    container = ServiceContainer()
    return build_graph(container)


if __name__ == "__main__":
    app = make_graph()
    logger.info("药妆智多星多智能体客服系统 v4.5 启动成功")
