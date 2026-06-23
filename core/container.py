"""
依赖注入容器（v5.1 — Protocol 类型注解）
管理所有系统组件的生命周期和依赖关系。

替代 multi_agent_customer_service.py 中的模块级全局变量，
提供显式的依赖管理，便于测试、替换和生命周期控制。

v4.5: 图构建统一委托给 build_graph(container)，
消除容器内 _build_graph() 的重复图拓扑定义。
v5.1: Protocol 类型注解替代 Any，编译期类型安全。

Usage:
    # 推荐方式：通过容器初始化
    container = ServiceContainer()
    await container.initialize()
    # container.graph_app 在 initialize() 中自动构建
"""

import asyncio
from typing import Any

from core.logger import get_logger
from core.protocols import (
    ERPProtocol,
    KnowledgeBaseProtocol,
    LLMProtocol,
    ToolRegistryProtocol,
)

logger = get_logger("core.container")


class ServiceContainer:
    """服务容器：管理所有系统服务实例的生命周期和依赖关系。

    容器分两个阶段初始化：
      1. __init__(): 同步创建无依赖的基础设施（bus, bb, cache 等）
      2. initialize(): 异步初始化有依赖的组件（LLM, Agents, Router, RAG 等）
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._initialized = False

        # ===== 基础设施（同步创建，无依赖）=====
        from core.config import (
            REDIS_URL,
            SESSION_STORAGE_BACKEND,
            SESSION_WINDOW_SIZE,
        )
        from core.message_bus import MessageBus
        from core.monitoring import CircuitBreaker, MetricsCollector, SLAAlertManager
        from core.shared_blackboard import SharedBlackboard

        self.bus = MessageBus()
        self.bb = SharedBlackboard()
        self.metrics = MetricsCollector()
        self.circuit_breaker = CircuitBreaker()
        self.sla_alert_mgr = SLAAlertManager(bus=self.bus)

        # v6.1: ResponseCache 延迟初始化（embedding / Redis / Qdrant 在 initialize() 中注入）
        self.cache = None

        # Session: 可选 Redis 持久化
        from core.session.session_manager import EnhancedSessionManager, default_session_manager

        self.session_mgr = default_session_manager
        if SESSION_STORAGE_BACKEND == "redis":
            try:
                self.session_mgr = EnhancedSessionManager(
                    storage_backend="redis",
                    window_size=SESSION_WINDOW_SIZE,
                    url=REDIS_URL,
                )
                # Redis 缓存预热移至 initialize()（异步执行）
                self._redis_url = REDIS_URL
            except Exception as e:
                logger.warning(f"Redis 初始化失败，回退到内存模式: {e}")

        # ===== 延迟初始化组件（initialize() 中设置）=====
        self.llm: LLMProtocol | None = None
        self.vision_llm: LLMProtocol | None = None

        # ERP
        self.erp: ERPProtocol | None = None

        # Agents
        self.agents_dict: dict[str, Any] = {}
        self.response_agent: Any = None

        # Session & Router
        self.router: Any = None

        # Orchestrator（依赖 bus + bb，已在上面创建）
        from collaboration.orchestrator import CollaborationOrchestrator

        self.orchestrator = CollaborationOrchestrator(self.bus, self.bb)

        # v5.2: LangGraph Checkpointer（对话状态持久化）
        try:
            from langgraph.checkpoint.memory import MemorySaver

            self.checkpointer = MemorySaver()
        except ImportError:
            self.checkpointer = None
            logger.debug("langgraph.checkpoint.memory 不可用，断点续传功能禁用")

        # RAG & Tools
        self.knowledge_base: KnowledgeBaseProtocol | None = None
        self._legacy_kb: Any = None  # v6.0: 并行运行时保留的 ChromaDB legacy 实例
        # v6.1: 容器级单例 Embedding 模型
        self.embedding_model: Any = None
        self.tool_registry: ToolRegistryProtocol | None = None

        # v5.1: Prompt 版本管理器
        self.prompt_manager: Any = None

        # v5.1: Token 用量追踪器
        self.token_tracker: Any = None

        # v4.1: LangGraph 应用实例
        self.graph_app: Any = None

    def _create_redis_client(self):
        """v6.1: 创建同步 Redis 客户端（可能失败返回 None）"""
        try:
            import redis
            from core.config import REDIS_URL

            client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
            client.ping()
            logger.info("Redis 客户端初始化成功")
            return client
        except Exception as e:
            logger.warning(f"Redis 不可用，L1 缓存将降级: {e}")
            return None

    async def initialize(self):
        """初始化所有延迟加载的组件（幂等，多次调用安全）。

        初始化顺序：
          1. LLM 客户端
          2. RAG 知识库 + 工具注册
          3. Agent 实例（含 ReActAgent）
          4. Router
          5. Orchestrator
        """
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            # 1. LLM
            await self._init_llm()

            # v5.5: LLM 健康检查 — 启动时验证端点是否可连接
            await self._check_llm_health()

            # 1.5. v5.1: Vision LLM（多模态模型，仅在启用时初始化）
            await self._init_vision_llm()

            # 1.6. v5.1: Token 用量追踪器
            await self._init_token_tracker()

            # 2. ERP
            if self.erp is None:
                from erp.factory import create_erp_adapter

                self.erp = create_erp_adapter()

            # 3. RAG + Tools
            await self._init_rag_and_tools()

            # 3.2. v6.1: 初始化 ResponseCache（注入 Redis / Qdrant / Embedding）
            await self._init_cache()

            # v6.1: 订阅主动失效事件
            if self.cache and self.bus:
                await self.cache.subscribe_to_bus(self.bus)

            # 3.5. v5.1: Prompt 版本管理器
            await self._init_prompt_manager()

            # 4. Agents
            await self._init_agents()

            # 5. Router
            await self._init_router()

            # 6. 构建 LangGraph 应用
            self._build_graph()

            self._initialized = True
            logger.info(f"ServiceContainer 初始化完成 ({len(self.agents_dict)} agents)")

    def _build_graph(self):
        """构建 LangGraph 工作流图（委托给 multi_agent_customer_service.build_graph）"""
        from core.graph_builder import build_graph

        if self.checkpointer is None:
            try:
                from langgraph.checkpoint.memory import MemorySaver

                self.checkpointer = MemorySaver()
            except ImportError:
                pass

        self.graph_app = build_graph(self, checkpointer=self.checkpointer)
        cp_status = "enabled" if self.checkpointer else "disabled"
        logger.info(f"[Container] LangGraph 构建完成 (checkpointer={cp_status})")

    # ===== 内部初始化方法 =====

    async def _init_llm(self):
        """初始化 LLM 客户端（v4.1: 智能降级 - API Key 无效时自动切换到规则引擎）"""
        if self.llm is not None:
            return
        from core.config import DEV_MODE, OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
        from llm.client import OpenAICompatibleClient

        # v4.1: 检查 API Key 是否有效（v5.5: 使用游标原则检测，防止 test-mock-key 等非生产 Key 绕过）
        _PLACEHOLDER_PREFIXES = ("your_", "test-", "mock-", "sk-placeholder", "sk-xxx", "sk-your")
        api_key_valid = bool(OPENAI_API_KEY) and not any(
            OPENAI_API_KEY.lower().startswith(p) for p in _PLACEHOLDER_PREFIXES
        )
        # 真实 API Key 至少 40 字符（SiliconFlow / OpenAI 等）
        if api_key_valid and len(OPENAI_API_KEY) < 40:
            api_key_valid = False
            if DEV_MODE:
                logger.warning(f"⚠️ API Key 长度异常（{len(OPENAI_API_KEY)} < 40），视为无效")

        if not api_key_valid and DEV_MODE:
            # 开发模式：API Key 无效时自动降级到规则引擎
            try:
                from llm.rule_based_llm import RuleBasedLLM

                logger.warning("⚠️ DeepSeek API Key 未配置，自动切换到规则引擎模式（开发降级）")
                logger.warning("💡 配置真实的 API Key：编辑 .env.dev 文件第 7 行")
                self.llm = RuleBasedLLM()
            except ImportError:
                logger.error("❌ 规则引擎模块不可用，请配置 API Key", exc_info=True)
                self.llm = OpenAICompatibleClient(
                    api_key=OPENAI_API_KEY or "invalid",
                    base_url=OPENAI_BASE_URL,
                    model=OPENAI_MODEL,
                    circuit_breaker=self.circuit_breaker,
                )
        else:
            # 生产模式或 API Key 有效：使用真实 LLM
            self.llm = OpenAICompatibleClient(
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_BASE_URL,
                model=OPENAI_MODEL,
                circuit_breaker=self.circuit_breaker,
            )

    async def _check_llm_health(self):
        """v5.5: 启动时 LLM 端点健康检查（非阻塞，仅记录日志）。

        规则引擎模式跳过检查。失败时仅记录警告（系统已有 RuleBasedLLM 作为运行时降级）。
        """
        from llm.rule_based_llm import RuleBasedLLM

        if isinstance(self.llm, RuleBasedLLM):
            return  # 规则引擎不需要检查

        try:
            from langchain_core.messages import HumanMessage

            logger.info("[HealthCheck] 正在检查 LLM 端点...")
            await asyncio.wait_for(self.llm.async_invoke([HumanMessage(content="hi")]), timeout=10.0)
            logger.info("[HealthCheck] ✅ LLM 端点连通正常")
        except asyncio.TimeoutError:
            logger.warning("[HealthCheck] ⚠️ LLM 端点超时（10s），系统将以降级模式运行")
        except Exception as e:
            logger.warning(f"[HealthCheck] ⚠️ LLM 端点不可用: {type(e).__name__}")
            logger.warning("[HealthCheck] 系统将使用 RuleBasedLLM 作为运行时降级")

    async def _init_vision_llm(self):
        """v5.1: 初始化 Vision LLM 客户端（仅在 MULTIMODAL_ENABLED 时）"""
        from core.config import (
            MULTIMODAL_ENABLED,
            OPENAI_API_KEY,
            OPENAI_BASE_URL,
            OPENAI_MODEL,
            VISION_API_KEY,
            VISION_BASE_URL,
            VISION_MODEL,
        )

        if not MULTIMODAL_ENABLED:
            return

        # 从环境变量获取，留空则复用默认 LLM 配置
        vision_model = VISION_MODEL or OPENAI_MODEL
        vision_base_url = VISION_BASE_URL or OPENAI_BASE_URL
        vision_api_key = VISION_API_KEY or OPENAI_API_KEY

        # 如果 Vision 模型与默认模型相同，复用同一个客户端
        if (
            vision_model == OPENAI_MODEL
            and vision_base_url == OPENAI_BASE_URL
            and vision_api_key == OPENAI_API_KEY
        ):
            self.vision_llm = self.llm
            logger.info("Vision LLM 复用默认 LLM 客户端")
            return

        from llm.client import OpenAICompatibleClient

        self.vision_llm = OpenAICompatibleClient(
            api_key=vision_api_key,
            base_url=vision_base_url,
            model=vision_model,
            circuit_breaker=self.circuit_breaker,
        )
        logger.info(f"Vision LLM 初始化完成: {vision_model} @ {vision_base_url}")

    async def _init_token_tracker(self):
        """v5.1: 初始化 Token 用量追踪器"""
        if self.token_tracker is not None:
            return
        from core.token_tracker import init_token_tracker

        self.token_tracker = init_token_tracker()
        logger.info("Token 用量追踪器初始化完成")

    async def _init_rag_and_tools(self):
        """初始化 RAG 知识库 + 工具注册（v6.0: 支持 Qdrant / ChromaDB 双模式）"""
        if self.knowledge_base is None:
            from core.config import (
                CLIP_ENABLED,
                QDRANT_GRPC_PORT,
                QDRANT_HOST,
                QDRANT_API_KEY,
                QDRANT_PORT,
                QDRANT_PREFER_GRPC,
                RAG_PERSIST_DIRECTORY,
                VECTOR_DB_MODE,
            )
            from rag.seed_data import (
                seed_complaint_knowledge,
                seed_faq,
                seed_product_knowledge,
                seed_supplementary_data,
                seed_tech_support,
            )

            if VECTOR_DB_MODE in ("qdrant_only", "parallel"):
                # v6.1: 加载容器级单例 Embedding 模型
                if self.embedding_model is None:
                    try:
                        from sentence_transformers import SentenceTransformer

                        self.embedding_model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
                        logger.info("容器级 Embedding 模型加载完成 (BAAI/bge-small-zh-v1.5)")
                    except Exception as e:
                        logger.warning(f"Embedding 模型加载失败: {e}")
                        self.embedding_model = None

                # Qdrant 模式
                from rag.qdrant_knowledge_base import QdrantKnowledgeBase

                self.knowledge_base = QdrantKnowledgeBase(
                    host=QDRANT_HOST,
                    port=QDRANT_PORT,
                    grpc_port=QDRANT_GRPC_PORT,
                    prefer_grpc=QDRANT_PREFER_GRPC,
                    api_key=QDRANT_API_KEY,
                    clip_enabled=CLIP_ENABLED,
                    embedding_model=self.embedding_model,  # v6.1
                )
                logger.info(
                    f"Qdrant 知识库初始化完成 (host={QDRANT_HOST}, mode={VECTOR_DB_MODE})"
                )

                if VECTOR_DB_MODE == "parallel":
                    from rag.legacy_chroma import ChromaKnowledgeBase

                    self._legacy_kb = ChromaKnowledgeBase(clip_enabled=CLIP_ENABLED)
                    logger.info("Legacy ChromaDB 知识库已初始化（并行模式）")
            else:
                # 兼容模式：使用 ChromaDB legacy
                from rag.legacy_chroma import ChromaKnowledgeBase

                self.knowledge_base = ChromaKnowledgeBase(clip_enabled=CLIP_ENABLED)
                logger.info("ChromaDB (legacy) 知识库初始化完成 (mode=chroma_legacy)")

            # 种子数据（所有模式通用）
            if RAG_PERSIST_DIRECTORY:
                logger.info("持久化模式，跳过种子数据")
            else:
                logger.info("内存模式，初始化种子数据")
                seed_product_knowledge(self.knowledge_base)
                seed_faq(self.knowledge_base)
                seed_tech_support(self.knowledge_base)
                seed_complaint_knowledge(self.knowledge_base)
                seed_supplementary_data(self.knowledge_base)

            logger.info(
                f"RAG 知识库初始化完成 "
                f"(product={self.knowledge_base.get_collection_count('product_knowledge')}, "
                f"faq={self.knowledge_base.get_collection_count('faq')}, "
                f"tech={self.knowledge_base.get_collection_count('tech_support')}, "
                f"complaint={self.knowledge_base.get_collection_count('complaint_knowledge')})"
            )

        if self.tool_registry is None:
            from tools.erp_tools import create_erp_tools

            if self.erp is None:
                from erp.factory import create_erp_adapter

                self.erp = create_erp_adapter()
            self.tool_registry = create_erp_tools(self.erp)
            logger.info(f"工具注册完成: {self.tool_registry.list_tools()}")

    async def _init_cache(self):
        """v6.1: 初始化三级缓存，注入外部依赖"""
        if self.cache is not None:
            return
        from cache.response_cache import ResponseCache
        from core.config import (
            CACHE_CLEANUP_INTERVAL,
            CACHE_FALLBACK_ENABLED,
            CACHE_FALLBACK_THRESHOLD,
            CACHE_QDRANT_COLLECTION,
            CACHE_QDRANT_MAX_POINTS,
            CACHE_TTL_POLICY,
            CACHE_VECTOR_SCORE_THRESHOLD,
        )

        redis_client = self._create_redis_client()
        qdrant_client = getattr(self.knowledge_base, "_client", None) if self.knowledge_base else None
        if qdrant_client is None and hasattr(self, "_legacy_kb") and self._legacy_kb:
            qdrant_client = getattr(self._legacy_kb, "_client", None)

        self.cache = ResponseCache(
            redis_client=redis_client,
            qdrant_client=qdrant_client,
            embedding_model=self.embedding_model,
            l1_ttl_policy=CACHE_TTL_POLICY,
            l2_collection=CACHE_QDRANT_COLLECTION,
            l2_threshold=CACHE_VECTOR_SCORE_THRESHOLD,
            l2_max_points=CACHE_QDRANT_MAX_POINTS,
            fallback_enabled=CACHE_FALLBACK_ENABLED,
            fallback_threshold=CACHE_FALLBACK_THRESHOLD,
        )

        # Inject cache into response_agent
        if self.response_agent and hasattr(self.response_agent, "cache"):
            self.response_agent.cache = self.cache

        # 后台缓存清理任务
        async def _cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(CACHE_CLEANUP_INTERVAL)
                    self.cache.cleanup_expired()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"缓存清理循环异常: {e}")

        self._cache_cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.info("ResponseCache 初始化完成 (L1=Redis L2=Qdrant L3=Jaccard)")

    async def _init_prompt_manager(self):
        """v5.1: 初始化 Prompt 版本管理器"""
        if self.prompt_manager is not None:
            return
        from core.prompt_manager import init_prompt_manager

        self.prompt_manager = init_prompt_manager()
        logger.info("Prompt 版本管理器初始化完成")

    async def _init_agents(self):
        """初始化所有 Agent 实例"""
        if self.agents_dict:
            return

        from agents import (
            BillingAgent,
            ComplaintAgent,
            GeneralAgent,
            ProductAgent,
            ReActAgent,
            TechAgent,
        )
        from core.config import ERP_MODE

        agent_classes = {
            "product_agent": ProductAgent,
            "tech_agent": TechAgent,
            "billing_agent": BillingAgent,
            "complaint_agent": ComplaintAgent,
            "general_agent": GeneralAgent,
        }

        for name, cls in agent_classes.items():
            agent = cls(llm=self.llm)
            agent.set_session_manager(self.session_mgr)
            agent.set_bus(self.bus)
            agent.set_blackboard(self.bb)
            agent.set_erp(self.erp)
            if self.vision_llm:  # v5.1: 注入 Vision LLM
                agent.set_vision_llm(self.vision_llm)
            if self.prompt_manager:  # v5.1: 注入 Prompt 版本管理器
                agent.set_prompt_manager(self.prompt_manager)
            self.agents_dict[name] = agent

        # RAG 注入到需要检索的 Agent
        for name in ("product_agent", "tech_agent", "complaint_agent"):
            if name in self.agents_dict:
                self.agents_dict[name].set_knowledge_base(self.knowledge_base)

        # ReAct 推理 Agent
        react_agent = ReActAgent(llm=self.llm)
        react_agent.set_session_manager(self.session_mgr)
        react_agent.set_bus(self.bus)
        react_agent.set_blackboard(self.bb)
        react_agent.set_erp(self.erp)
        react_agent.set_knowledge_base(self.knowledge_base)
        react_agent.set_tool_registry(self.tool_registry)
        if self.vision_llm:  # v5.1: 注入 Vision LLM
            react_agent.set_vision_llm(self.vision_llm)
        if self.prompt_manager:  # v5.1: 注入 Prompt 版本管理器
            react_agent.set_prompt_manager(self.prompt_manager)
        self.agents_dict["react_agent"] = react_agent

        # ResponseAgent
        from agents import ResponseAgent as _ResponseAgent

        self.response_agent = _ResponseAgent(
            session_manager=self.session_mgr,
            message_bus=self.bus,
            blackboard=self.bb,
            cache=self.cache,
        )
        self.response_agent.set_llm(self.llm)

        logger.info(
            f"初始化 {len(self.agents_dict)} 个 Agent (含 ReActAgent) 完成 (ERP_MODE={ERP_MODE})"
        )

    async def _init_router(self):
        """初始化查询路由器"""
        if self.router is not None:
            return
        from core.config import ROUTING_COMPLEXITY_THRESHOLD
        from router.query_router import QueryRouter

        self.router = QueryRouter(
            llm=self.llm,
            complexity_threshold=ROUTING_COMPLEXITY_THRESHOLD,
        )

    async def close(self):
        """P1-3: 优雅关闭，按依赖逆序释放资源"""
        if not self._initialized:
            return

        logger.info("ServiceContainer 开始关闭...")

        # 0. v6.1: 停止缓存清理任务
        if hasattr(self, "_cache_cleanup_task") and self._cache_cleanup_task:
            self._cache_cleanup_task.cancel()
            try:
                await self._cache_cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
            logger.info("  ✅ 缓存清理任务已停止")

        # 0.1. v6.1: 关闭 Redis 连接
        if self.cache and hasattr(self.cache, "_redis") and self.cache._redis:
            try:
                self.cache._redis.close()
                logger.info("  ✅ Redis 连接已关闭")
            except Exception as e:
                logger.warning(f"  ⚠️ Redis 关闭异常: {e}")

        # 1. 关闭 LLM 连接池
        try:
            from llm.client import OpenAICompatibleClient

            await OpenAICompatibleClient.close_all_clients()
            logger.info("  ✅ LLM 连接池已关闭")
        except Exception as e:
            logger.warning(f"  ⚠️ LLM 连接池关闭异常: {e}")

        # 2. 关闭 ERP 适配器
        if self.erp and hasattr(self.erp, "close"):
            try:
                await self.erp.close()
                logger.info("  ✅ ERP 适配器已关闭")
            except Exception as e:
                logger.warning(f"  ⚠️ ERP 适配器关闭异常: {e}")

        # 3. 重置状态
        self._initialized = False
        self.graph_app = None
        logger.info("ServiceContainer 关闭完成")
