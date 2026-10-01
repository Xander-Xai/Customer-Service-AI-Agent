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

import asyncio
import contextlib
import time

from langgraph.graph import StateGraph

from core.container import ServiceContainer
from core.logger import get_logger, set_trace_id
from core.state import AgentState
from router.query_router import RoutingResult

logger = get_logger("graph")


def _format_duration(seconds: float) -> str:
    """格式化耗时显示"""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


async def _emit_status(state: AgentState | dict, phase: str, message: str) -> None:
    """图节点状态 emit 辅助函数（v6.0: 全链路 SSE 流式）

    从 state 中获取 stream_callback，发出 status 事件。
    回调不存在或抛出异常时不传播，仅 debug log。
    """
    cb = state.get("stream_callback") if isinstance(state, dict) else None
    if cb:
        try:
            await cb({"type": "status", "phase": phase, "content": message})
        except Exception:
            logger.debug(f"_emit_status: callback failed (phase={phase})", exc_info=True)


# ===== v4.5: 唯一图构建入口 =====


def build_graph(container: ServiceContainer, checkpointer=None):
    """通过 ServiceContainer 构建 LangGraph 工作流图（唯一图构建入口）

    三层状态机架构：
      Layer 0: Cache Check（check_cache）— 缓存命中直接跳到 final_response
      Layer 1: Router（classify_query_node）— 双层意图识别 + 复杂度评分
      Layer 2: Expert Agent（5 种协作模式节点）— 动态路由选择（含 ReAct 推理）
      Layer 3: ResponseAgent（final_response_node）— 缓存/会话/SLA/事件

    v4.3: 运行时模式升级（sequential -> consultation -> parallel -> react）
    v5.2: 支持 checkpointer 参数，实现对话状态持久化和断点续传

    Args:
        container: ServiceContainer 依赖注入容器
        checkpointer: LangGraph Checkpointer（生产 AsyncPostgresSaver /
            开发 MemorySaver），None 则不启用持久化

    Usage:
        container = ServiceContainer()
        await container.initialize()
        app = build_graph(container, checkpointer=container.checkpointer)
        # thread_id == session_id 实现对话续传
        config = {"configurable": {"thread_id": session_id}}
        result = await app.ainvoke(state, config=config)
    """
    c = container  # 短别名，闭包捕获

    # ---- 容器绑定的图节点 ----

    async def _classify_query_node(state: AgentState) -> AgentState:
        """双层路由节点：LLM Router + Rule Classifier + 复杂度评分（v3.2: 熔断器降级）
        v4.1: 从 state 传播 trace_id 到 contextvars
        v5.4: RAG 预取与路由并行执行
        """
        trace_id = state.get("trace_id", "")
        if trace_id:
            set_trace_id(trace_id)

        await _emit_status(state, "classify", "📋 正在分类问题...")

        query = state["customer_query"]
        session_id = state.get("session_id", "default")

        # v6.3: 并行化独立的初始化操作 — session 创建、router 初始化、上下文获取
        # session 创建 + router 初始化 互相独立，先并行执行
        init_tasks = [c.session_mgr.create_session(session_id)]
        if c.router is None:
            init_tasks.append(c._init_router())
        await asyncio.gather(*init_tasks)

        context = await c.session_mgr.get_conversation_context(session_id)
        context_text = "\n".join([m.get("content", "") for m in context[-6:]]) if context else ""

        # v5.4: RAG 预取 — 与路由并行执行，提前检索知识库
        rag_prefetch_task = asyncio.create_task(_rag_prefetch(query))

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
                result = await c.router.route(
                    query,
                    context_text,
                    user_id=state.get("user_id"),
                )
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

        # 等待 RAG 预取结果
        rag_context = await rag_prefetch_task
        if rag_context:
            state["_rag_prefetch"] = rag_context

        state["query_type"] = result.query_type
        state["current_agent"] = result.agent_name
        state["complexity"] = result.complexity
        state["fast_path"] = result.fast_path
        state["collaboration_mode"] = ""

        logger.info(
            f"[Router] type={result.query_type} agent={result.agent_name} "
            f"complexity={result.complexity} fast_path={result.fast_path}"
        )

        # v6.3: 黑板写入 fire-and-forget（不阻塞主流程）
        asyncio.create_task(c.bb.write(
            "last_routing",
            {
                "query_type": result.query_type,
                "agent": result.agent_name,
                "complexity": result.complexity,
            },
        ))

        await _emit_status(state, "classify", f"📋 分类结果: {result.query_type} (agent={result.agent_name})")

        return state

    async def _rag_prefetch(query: str) -> dict | None:
        """RAG 预取：用原始查询提前检索知识库，与路由并行执行

        返回检索到的上下文文本，供 Agent 直接使用（跳过 Agent 内部的 RAG 检索）。
        失败时返回 None，Agent 会回退到自己的 RAG 检索。
        """
        if not c.knowledge_base:
            return None
        try:
            return await asyncio.wait_for(
                c.knowledge_base.prefetch_reusable(query, c.llm),
                timeout=2.0,
            )
        except Exception as e:
            logger.debug(f"[RAG预取] 失败（Agent 将自行检索）: {e}")
        return None

    async def _check_cache_node(state: AgentState) -> AgentState:
        """缓存检查节点（v6.0: SSE 状态流式 + 缓存伪流式）"""
        query = state["customer_query"]

        await _emit_status(state, "cache", "🔍 检查缓存中...")

        if c.cache is not None:
            # P0-02: 读取端携带可信 user_id，缓存按 read_scope_keys 探测
            # SHARED + 调用方作用域。读取发生在路由分类前，intent 未知，故不传 intent_type。
            cache_metadata = {
                "user_id": state.get("user_id"),
            }
            cached = c.cache.get(query, metadata=cache_metadata)
        else:
            cached = None
        if cached:
            await _emit_status(state, "cache", "⚡ 缓存命中，快速响应中...")

            # v6.0: 缓存伪流式 — 分块输出缓存内容
            stream_callback = state.get("stream_callback")
            if stream_callback:
                chunk_size = 20
                interval = 0.03
                for i in range(0, len(cached), chunk_size):
                    try:
                        await stream_callback({"type": "chunk", "content": cached[i:i + chunk_size]})
                    except Exception:
                        logger.debug("cache pseudo-stream callback failed")
                    await asyncio.sleep(interval)

            state["response"] = cached
            state["cached"] = True
            state["current_agent"] = "cache"
            state["collaboration_mode"] = "cache_hit"
            logger.info(f"[Cache] HIT: {query[:30]}...")
        else:
            await _emit_status(state, "cache", "🔍 L1 未命中，进行语义匹配...")
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

            # v6.0: emit agent_switch 和 mode 事件
            agent_name = routing_result.agent_name
            await _emit_status(state, "route", f"🔄 协作模式: {mode_name}, Agent: {agent_name}")
            cb = state.get("stream_callback")
            if cb:
                with contextlib.suppress(Exception):
                    await cb({"type": "agent_switch", "from": "router", "to": agent_name})

            result = await mode.execute(c.agents_dict, dict(state), ctx)
        except Exception as e:
            logger.error(f"[{mode_name}] error: {e}", exc_info=True)
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
                logger.error(f"[ResponseAgent] error: {e}, 降级到基础处理", exc_info=True)
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
                                # v6.1: 写入缓存时携带完整 metadata
                                # P0-02: 携带可信 user_id，个性化回答进入用户作用域
                                cache_meta = {
                                    "intent_type": state.get("query_type", "default"),
                                    "user_id": state.get("user_id"),
                                }
                                c.cache.put(state["customer_query"], state["response"], metadata=cache_meta)
                            logger.info(f"[ModeUpgrade] 升级重试完成: mode={new_mode}")
                    except Exception as e:
                        logger.error(f"[ModeUpgrade] 升级重试失败: {e}，保留原响应", exc_info=True)
        else:
            logger.warning("[ResponseAgent] 未初始化，使用基础后处理")
            _fallback_post_process(state)

        return state

    def _fallback_post_process(state: AgentState):
        """降级后处理：仅做缓存写入"""
        if state.get("response") and not state.get("cached", False) and c.cache:
            # v6.1: 写入缓存时携带完整 metadata
            # P0-02: 携带可信 user_id，个性化回答进入用户作用域
            cache_meta = {
                "intent_type": state.get("query_type", "default"),
                "user_id": state.get("user_id"),
                "product_id": state.get("extracted_entities", {}).get("product_id"),
            }
            c.cache.put(state["customer_query"], state["response"], metadata=cache_meta)

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

    # v5.2: 支持 checkpointer 实现对话状态持久化
    compile_kwargs = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer
    app = workflow.compile(**compile_kwargs)
    logger.info("[Graph] LangGraph v4.5 构建完成 - 缓存前置 + 5种协作模式 + RAG + 模式升级")
    return app


# ===== 向后兼容：make_graph() 包装器 =====


_default_container = None


def make_graph():
    """向后兼容包装器：内部使用单例 ServiceContainer 并调用其 _build_graph()。

    注意：此函数会使用一个单例的 ServiceContainer 实例，
    仅包含同步初始化的基础设施组件（不包含 LLM/Agents/Router 等异步组件）。
    图节点会在首次调用时懒初始化所需组件。

    checkpointer 由容器生命周期决定：生产需 postgres 后端，开发/测试回退
    MemorySaver（由 ``_build_graph`` 保证，生产缺失会 fail closed）。
    此入口服务于 ``langgraph.json`` 本地开发图，不用于生产编排。
    """
    global _default_container
    if _default_container is None:
        _default_container = ServiceContainer()
    _default_container._build_graph()
    return _default_container.graph_app


if __name__ == "__main__":
    app = make_graph()
    logger.info("药妆智多星多智能体客服系统 v4.5 启动成功")
