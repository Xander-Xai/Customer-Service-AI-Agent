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
import contextlib
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

        # v6.3: ResponseCache 基础实例（无 Redis/Qdrant），initialize() 中替换为完整版
        from cache.response_cache import ResponseCache

        self.cache = ResponseCache()

        # Session: 可选 Redis 持久化
        from core.session.session_manager import EnhancedSessionManager, default_session_manager

        self.session_mgr = default_session_manager
        if SESSION_STORAGE_BACKEND == "redis":
            try:
                mgr = EnhancedSessionManager(
                    storage_backend="redis",
                    window_size=SESSION_WINDOW_SIZE,
                    url=REDIS_URL,
                )
                # 构造 EnhancedSessionManager **不会**连接 Redis：_get_redis() 是懒加载的，
                # 而且它自己吞掉连接异常并把 storage_backend 改成 "memory"。因此下面这个
                # try/except 过去永远抓不到生产 Redis 故障——进程正常启动，到第一个请求才
                # 静默降级成进程内 memory（多 worker 分片、重启丢失）。
                # 这里显式 probe：生产必须在启动阶段就失败。
                self._assert_session_redis_ready(mgr, REDIS_URL)
                self.session_mgr = mgr
                # Redis 缓存预热移至 initialize()（异步执行）
                self._redis_url = REDIS_URL
            except Exception as e:
                # 生产：Redis Session 初始化失败必须 fail-fast，绝不静默回退进程内
                # memory（gunicorn 多 worker 会分片、重启丢失）。
                from core.config import DEV_MODE
                from core.config import ConfigurationError as _CfgError

                if not DEV_MODE:
                    raise _CfgError(
                        "Production requires a working Redis for "
                        "SESSION_STORAGE_BACKEND=redis; refusing to fall back to "
                        f"in-process memory ({type(e).__name__})"
                    ) from e
                logger.warning(f"Redis 初始化失败，开发环境回退到内存模式: {e}")

        # ===== 延迟初始化组件（initialize() 中设置）=====
        self.llm: LLMProtocol | None = None
        self.vision_llm: LLMProtocol | None = None

        # ERP
        self.erp: ERPProtocol | None = None
        # P0-03: ERP 授权边界（包装 self.erp）。Agent / Tool 经此访问私人 ERP 资源。
        self._erp_authz: Any = None
        self._erp_authz_source: Any = None  # 已包装的原始 adapter（identity 校验）

        # Agents
        self.agents_dict: dict[str, Any] = {}
        self.response_agent: Any = None

        # Session & Router
        self.router: Any = None

        # Orchestrator（依赖 bus + bb，已在上面创建）
        from collaboration.orchestrator import CollaborationOrchestrator

        self.orchestrator = CollaborationOrchestrator(self.bus, self.bb)

        # LangGraph Checkpointer：生命周期在 initialize() 中显式管理
        # （内存/PostgreSQL 由 LANGGRAPH_CHECKPOINT_BACKEND 决定），
        # 不再在 __init__ 中隐式创建 MemorySaver。
        self.checkpointer: Any = None
        self._checkpoint_runtime: Any = None

        # RAG & Tools
        self.knowledge_base: KnowledgeBaseProtocol | None = None
        # v6.1: 容器级单例 Embedding 模型
        self.embedding_model: Any = None
        self.tool_registry: ToolRegistryProtocol | None = None

        # v5.1: Prompt 版本管理器
        self.prompt_manager: Any = None

        # v5.1: Token 用量追踪器
        self.token_tracker: Any = None

        # v4.1: LangGraph 应用实例
        self.graph_app: Any = None

        # v6.1: 组件注册表（用于组件计数 + 服务发现）
        self._services: dict[str, Any] = {}

    @staticmethod
    def _assert_session_redis_ready(mgr: object, redis_url: str) -> None:
        """启动阶段主动探测 Session Redis；不可用则抛错（生产 -> fail-fast）。

        不能只依赖 ``EnhancedSessionManager._get_redis()``：它在连接失败时把自己降级成
        memory 并返回 None，静默得很。这里用**独立**的临时连接做 ping，失败就把异常
        抛给调用方。

        同时校验没有发生降级：万一 ``mgr.storage_backend`` 已经是 memory（说明刚才
        某处触发了懒加载并降级），也视为失败。
        """
        import redis as _redis

        client = _redis.Redis.from_url(
            redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3
        )
        try:
            client.ping()
        finally:
            with contextlib.suppress(Exception):
                client.close()

        backend = getattr(mgr, "storage_backend", None)
        if backend is not None and backend != "redis":
            raise RuntimeError(
                f"session manager storage_backend degraded to {backend!r} "
                "despite a successful Redis ping"
            )

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

            # 5.5. LangGraph Checkpointer（必须在图编译前 ready）
            await self._init_checkpointer()

            # 6. 构建 LangGraph 应用
            self._build_graph()

            self._initialized = True
            # v6.1: 构建组件注册表
            self._rebuild_service_registry()
            # v6.1: 设置活跃组件 Prometheus 指标
            try:
                from core.monitoring import active_components_total
                active_components_total.set(len(self._services))
            except Exception:
                pass
            logger.info(
                f"ServiceContainer 初始化完成 ({len(self.agents_dict)} agents, "
                f"{len(self._services)} services)"
            )

    def _build_graph(self):
        """构建 LangGraph 工作流图（委托给 core.graph_builder.build_graph）

        前置条件：checkpointer 必须已 ready（由 _init_checkpointer() 在
        initialize() 中保证）。生产环境缺失 checkpointer 视为配置错误，
        绝不临时降级到 MemorySaver。
        """
        from core.graph_builder import build_graph

        if self.checkpointer is None:
            from core.config import DEV_MODE

            if not DEV_MODE:
                from core.config import ConfigurationError

                raise ConfigurationError(
                    "生产环境图构建前 LangGraph checkpointer 未就绪；"
                    "请检查 LANGGRAPH_CHECKPOINT_BACKEND / 数据库配置"
                )
            # 开发/测试的向后兼容路径（例如直接构造容器而不调用 initialize()）
            self._ensure_memory_checkpointer()

        self.graph_app = build_graph(self, checkpointer=self.checkpointer)
        runtime = self._checkpoint_runtime
        backend = getattr(runtime, "backend", "none")
        logger.info(f"[Container] LangGraph 构建完成 (checkpointer={backend})")

    def _ensure_memory_checkpointer(self):
        """同步兜底：在开发/测试中确保存在 MemorySaver（不用于生产）。"""
        if self.checkpointer is not None:
            return
        from core.checkpointer import build_memory_checkpointer

        self._checkpoint_runtime = build_memory_checkpointer()
        self.checkpointer = self._checkpoint_runtime.checkpointer

    async def _init_checkpointer(self, *, backend=None, database_url=None, dev_mode=None):
        """初始化 LangGraph checkpoint 后端（显式生命周期）。

        - backend=postgres：创建官方 AsyncPostgresSaver，失败时生产 fail closed。
        - backend=memory：进程内 MemorySaver（开发/测试）。
        - 开发环境下 postgres 初始化失败会降级到 MemorySaver，但显式记录
          status=degraded 并暴露到健康检查，绝不静默。
        """
        from core import config as cfg
        from core.checkpointer import (
            CheckpointBackendError,
            build_memory_checkpointer,
            build_postgres_checkpointer,
        )

        if backend is None:
            backend = cfg.LANGGRAPH_CHECKPOINT_BACKEND
        if dev_mode is None:
            dev_mode = cfg.DEV_MODE
        if database_url is None:
            database_url = cfg.derive_checkpoint_database_url(
                cfg.LANGGRAPH_CHECKPOINT_DATABASE_URL, database_url=cfg.DATABASE_URL
            )

        try:
            if backend == "postgres":
                self._checkpoint_runtime = await build_postgres_checkpointer(
                    database_url,
                    min_size=cfg.LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE,
                    max_size=cfg.LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE,
                    setup_timeout=cfg.LANGGRAPH_CHECKPOINT_SETUP_TIMEOUT,
                )
            elif backend == "memory":
                self._checkpoint_runtime = build_memory_checkpointer()
            else:
                raise CheckpointBackendError(f"未知 checkpoint backend: {backend}")
        except Exception as e:
            if not dev_mode:
                logger.error(
                    "生产环境 LangGraph checkpoint 初始化失败 (backend=%s): %s",
                    backend,
                    type(e).__name__,
                )
                raise cfg.ConfigurationError(
                    f"生产环境 LangGraph checkpoint 初始化失败（backend={backend}）："
                    "拒绝回退 MemorySaver，请修复数据库连通性/配置后重启"
                ) from e
            logger.warning(
                "开发环境 checkpoint 后端 %s 初始化失败，显式降级 MemorySaver: %s",
                backend,
                type(e).__name__,
            )
            self._checkpoint_runtime = build_memory_checkpointer()
            self._checkpoint_runtime.status = "degraded"
            self._checkpoint_runtime.detail = type(e).__name__

        self.checkpointer = self._checkpoint_runtime.checkpointer

    async def _close_checkpointer(self):
        """释放 checkpoint 后端资源（连接池），幂等。"""
        runtime = self._checkpoint_runtime
        if runtime is None:
            return
        from core.checkpointer import close_checkpoint_runtime

        try:
            await close_checkpoint_runtime(runtime)
            logger.info("  ✅ LangGraph checkpoint 后端已关闭")
        except Exception as e:
            logger.warning(f"  ⚠️ checkpoint 后端关闭异常: {e}")
        finally:
            self._checkpoint_runtime = None
            self.checkpointer = None

    # ===== 内部初始化方法 =====

    async def _init_llm(self):
        """初始化 LLM 客户端（v4.1: 智能降级 - API Key 无效时自动切换到规则引擎）

        key 是否可用由 :func:`core.config.evaluate_llm_api_key` 唯一判定 —— 与
        ``/api/health`` 读的是同一个函数（issue #51）。此前这里内联了一套
        placeholder 前缀黑名单 + 长度规则，health 端点另写了一套更弱的，两套在
        ``your_`` / ``your-``、``test-`` / ``mock-`` 的归属上都不一致，会出现
        「health 说 key 有效、进程却在跑 RuleBasedLLM」的组合。
        """
        if self.llm is not None:
            return
        from core.config import (
            DEV_MODE,
            LLM_KEY_REASON_TOO_SHORT,
            LLM_PROVIDER,
            OPENAI_API_KEY,
            OPENAI_BASE_URL,
            OPENAI_MODEL,
            evaluate_llm_api_key,
        )
        from llm.client import OpenAICompatibleClient

        # v4.1: 检查 API Key 是否有效（v5.5: 游标原则检测，防止 test-mock-key 等非生产 Key 绕过）
        # 唯一权威判定，纯函数、不发网络请求；长度下限 40 也在这里（此前只有本路径有）。
        key_status = evaluate_llm_api_key(OPENAI_API_KEY)
        api_key_valid = key_status.usable
        if not api_key_valid and key_status.configured and key_status.reason == LLM_KEY_REASON_TOO_SHORT and DEV_MODE:
            logger.warning(
                f"⚠️ API Key 长度异常（{key_status.length} < 40），视为无效"
            )

        if not api_key_valid and DEV_MODE:
            # 开发模式：API Key 无效时自动降级到规则引擎
            try:
                from llm.rule_based_llm import RuleBasedLLM

                # provider 名取自 LLM_PROVIDER，而不是写死某个供应商：写死过一次
                # （"DeepSeek API Key 未配置"），而默认 LLM_PROVIDER 是 siliconflow，
                # 于是这条告警在默认配置下直接说错了供应商（issue #46）。
                logger.warning(
                    f"⚠️ LLM API Key 不可用（provider={LLM_PROVIDER}），"
                    "自动切换到规则引擎模式（开发降级）"
                )
                # 指向环境变量名，不指向某个文件的具体行号：.env.dev 不入库、
                # 行号随文件改动漂移，指过去运维找不到东西。
                logger.warning("💡 配置真实的 API Key：在当前环境文件中设置 OPENAI_API_KEY")
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
        """初始化 RAG 知识库 + 工具注册（v6.2: 仅 Qdrant）"""
        if self.knowledge_base is None:
            from core.config import (
                CLIP_ENABLED,
                QDRANT_API_KEY,
                QDRANT_GRPC_PORT,
                QDRANT_HOST,
                QDRANT_PORT,
                QDRANT_PREFER_GRPC,
                RAG_PERSIST_DIRECTORY,
            )
            from rag.seed_data import (
                seed_complaint_knowledge,
                seed_faq,
                seed_product_knowledge,
                seed_supplementary_data,
                seed_tech_support,
            )

            # v6.2: 加载容器级单例 Embedding 模型
            #
            # issue #52: 选择交给 rag.embedding_factory 这唯一实现，不再在这里
            # "try 构造 ApiEmbedding"。旧写法有两个问题：(1) 构造几乎不会失败，
            # 所以 except 永远走不到；(2) 更要命的是它不判凭据是否真实 ——
            # EMBEDDING_API_KEY=sk-placeholder-... 是真值，于是拿着占位凭据
            # 真的去连 api.siliconflow.cn，默认测试 lane 因此需要出网。
            if self.embedding_model is None:
                try:
                    from rag.embedding_factory import select_embed_fn

                    selection = select_embed_fn()
                    self.embedding_model = selection.embed_fn
                    if self.embedding_model is None:
                        logger.info(
                            "容器级 Embedding 模型未启用（%s）：向量通道禁用，"
                            "检索降级到词法/BM25 通道",
                            selection.reason,
                        )
                    else:
                        logger.info(
                            f"容器级 Embedding 模型加载完成 (provider={selection.provider}, "
                            f"reason={selection.reason})"
                        )
                except Exception as e:
                    logger.warning(f"Embedding 模型选择失败: {e}")
                    self.embedding_model = None

            # Qdrant 知识库
            from rag.qdrant_knowledge_base import QdrantKnowledgeBase

            self.knowledge_base = QdrantKnowledgeBase(
                host=QDRANT_HOST,
                port=QDRANT_PORT,
                grpc_port=QDRANT_GRPC_PORT,
                prefer_grpc=QDRANT_PREFER_GRPC,
                api_key=QDRANT_API_KEY,
                clip_enabled=CLIP_ENABLED,
                embedding_model=self.embedding_model,
            )
            logger.info(
                f"Qdrant 知识库初始化完成 (host={QDRANT_HOST})"
            )

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

            # Rebuild BM25 from persisted Qdrant data so restart restores the
            # lexical channel explicitly. A timeout supersedes in-flight
            # publication and leaves retrieval honestly degraded.
            import functools

            from core.config import BM25_COLLECTIONS, BM25_REBUILD_TIMEOUT
            from rag.bm25_lifecycle import (
                REASON_REBUILD_EXCEPTION,
                REASON_REBUILD_TIMEOUT,
                BM25Readiness,
            )

            loop = asyncio.get_running_loop()
            rebuild_fn = functools.partial(
                self.knowledge_base.rebuild_bm25_from_qdrant,
                BM25_COLLECTIONS,
                timeout=BM25_REBUILD_TIMEOUT,
            )
            try:
                meta = await asyncio.wait_for(
                    loop.run_in_executor(None, rebuild_fn),
                    timeout=BM25_REBUILD_TIMEOUT + 2.0,
                )
                if getattr(meta, "status", None) is BM25Readiness.DEGRADED:
                    logger.warning("BM25 rebuild degraded: %s", getattr(meta, "reason", ""))
            except asyncio.TimeoutError:
                logger.warning("BM25 rebuild exceeded safety timeout")
                self.knowledge_base.supersede_bm25_rebuild(REASON_REBUILD_TIMEOUT)
            except Exception as e:
                logger.warning("BM25 rebuild failed: %s", e, exc_info=True)
                self.knowledge_base.supersede_bm25_rebuild(REASON_REBUILD_EXCEPTION)

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
            self.tool_registry = create_erp_tools(self._get_erp_authz())
            logger.info(f"工具注册完成: {self.tool_registry.list_tools()}")

    async def _init_cache(self):
        """v6.1: 初始化三级缓存，注入外部依赖"""
        if self.cache is not None:
            return
        from cache.response_cache import ResponseCache
        from core.config import (
            CACHE_CLEANUP_INTERVAL,
            CACHE_CONTENT_VERSION,
            CACHE_FALLBACK_ENABLED,
            CACHE_FALLBACK_THRESHOLD,
            CACHE_QDRANT_COLLECTION,
            CACHE_QDRANT_MAX_POINTS,
            CACHE_TTL_POLICY,
            CACHE_VECTOR_SCORE_THRESHOLD,
        )

        redis_client = self._create_redis_client()
        qdrant_client = getattr(self.knowledge_base, "_client", None) if self.knowledge_base else None

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
            content_version=CACHE_CONTENT_VERSION,
        )

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

    def _get_erp_authz(self):
        """P0-03: 用授权边界（ErpAuthorizationService）包装原始 ERP 适配器，
        供 Agent 与 ERP Tool 使用。授权在 Service/Tool 层强制执行，不依赖
        prompt 或模型参数。

        - self.erp 为 None 时返回 None（保留未初始化 ERP 的既有行为，避免
          影响不调用 initialize() 的测试）。
        - self.erp 已是 ErpAuthorizationService 时原样返回。
        - self.erp 被替换时按 identity 重新包装，避免缓存陈旧引用。
        """
        if self.erp is None:
            return None
        from erp.authorization import ErpAuthorizationService

        if isinstance(self.erp, ErpAuthorizationService):
            return self.erp
        if self._erp_authz_source is not self.erp:
            self._erp_authz = ErpAuthorizationService(self.erp)
            self._erp_authz_source = self.erp
        return self._erp_authz

    async def _init_agents(self):
        """初始化所有 Agent 实例"""
        if self.agents_dict:
            return

        from agents import (
            AftersalesAgent,
            BillingAgent,
            ComplaintAgent,
            GeneralAgent,
            ProductAgent,
            ReActAgent,
            SalesAgent,
            TechAgent,
        )
        from core.config import (
            ERP_MODE,
            REDIS_URL,
            TOOL_RESULT_CACHE_ENABLED,
            TOOL_RESULT_OFFLOAD_ENABLED,
        )
        from core.tool_result_cache import RedisToolResultCache
        from core.tool_result_store import RedisToolResultStore

        tool_result_store = None
        if TOOL_RESULT_OFFLOAD_ENABLED:
            tool_result_store = RedisToolResultStore(
                redis_client=getattr(self.cache, "_redis", None), redis_url=REDIS_URL
            )
        tool_result_cache = None
        if TOOL_RESULT_CACHE_ENABLED:
            tool_result_cache = RedisToolResultCache(
                redis_client=getattr(self.cache, "_redis", None), redis_url=REDIS_URL
            )

        agent_classes = {
            "product_agent": ProductAgent,
            "tech_agent": TechAgent,
            "billing_agent": BillingAgent,
            "complaint_agent": ComplaintAgent,
            "general_agent": GeneralAgent,
            "sales_agent": SalesAgent,
            "aftersales_agent": AftersalesAgent,
        }

        for name, cls in agent_classes.items():
            agent = cls(llm=self.llm)
            agent.set_session_manager(self.session_mgr)
            agent.set_bus(self.bus)
            agent.set_blackboard(self.bb)
            agent.set_erp(self._get_erp_authz())
            if self.vision_llm:  # v5.1: 注入 Vision LLM
                agent.set_vision_llm(self.vision_llm)
            if self.prompt_manager:  # v5.1: 注入 Prompt 版本管理器
                agent.set_prompt_manager(self.prompt_manager)
            if tool_result_store:
                agent.set_tool_result_store(tool_result_store)
            if tool_result_cache:
                agent.set_tool_result_cache(tool_result_cache)
            self.agents_dict[name] = agent

        # RAG 注入到需要检索的 Agent
        for name in ("product_agent", "tech_agent", "complaint_agent", "sales_agent", "aftersales_agent"):
            if name in self.agents_dict:
                self.agents_dict[name].set_knowledge_base(self.knowledge_base)

        # ReAct 推理 Agent
        react_agent = ReActAgent(llm=self.llm)
        react_agent.set_session_manager(self.session_mgr)
        react_agent.set_bus(self.bus)
        react_agent.set_blackboard(self.bb)
        react_agent.set_erp(self._get_erp_authz())
        react_agent.set_knowledge_base(self.knowledge_base)
        react_agent.set_tool_registry(self.tool_registry)
        if self.vision_llm:  # v5.1: 注入 Vision LLM
            react_agent.set_vision_llm(self.vision_llm)
        if self.prompt_manager:  # v5.1: 注入 Prompt 版本管理器
            react_agent.set_prompt_manager(self.prompt_manager)
        if tool_result_store:
            react_agent.set_tool_result_store(tool_result_store)
        if tool_result_cache:
            react_agent.set_tool_result_cache(tool_result_cache)
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

    def _rebuild_service_registry(self):
        """v6.1: 构建显式组件注册表，替代 dir() 猜测。

        注册所有核心服务到 _services 字典，支持组件计数和服务发现。
        每次 initialize() 完成后调用以刷新注册表。
        """
        self._services = {}

        # 核心基础设施
        self._services["message_bus"] = self.bus
        self._services["shared_blackboard"] = self.bb
        self._services["metrics_collector"] = self.metrics
        self._services["circuit_breaker"] = self.circuit_breaker
        self._services["sla_alert_manager"] = self.sla_alert_mgr

        # 会话管理
        self._services["session_manager"] = self.session_mgr

        # LLM
        if self.llm is not None:
            self._services["llm_client"] = self.llm
            from llm.rule_based_llm import RuleBasedLLM
            if isinstance(self.llm, RuleBasedLLM):
                self._services["rule_llm"] = self.llm

        # RAG 知识库
        if self.knowledge_base is not None:
            self._services["knowledge_base"] = self.knowledge_base

        # 缓存
        if self.cache is not None:
            self._services["cache"] = self.cache

        # LangGraph Checkpoint 后端
        if self.checkpointer is not None:
            self._services["checkpointer"] = self.checkpointer

        # ERP
        if self.erp is not None:
            self._services["erp_adapter"] = self.erp

        # Agent 系统
        if self.router is not None:
            self._services["query_router"] = self.router
        if self.orchestrator is not None:
            self._services["orchestrator"] = self.orchestrator
        if self.response_agent is not None:
            self._services["response_agent"] = self.response_agent
        self._services["total_agents"] = self.agents_dict  # len 穿透

        # 工具
        if self.tool_registry is not None:
            self._services["tool_registry"] = self.tool_registry

        # 其他 v6.1 注册组件
        try:
            from core.ab_testing import ABTestManager
            ab_mgr = ABTestManager()
            self._services["ab_test_manager"] = ab_mgr
            self._services["ab_tester"] = ab_mgr  # 规范别名
        except Exception:
            pass  # ABTestManager 可选

        # InputSanitizer 和 RateLimiter 由中间件层提供，容器内注册轻量代理
        self._services["input_sanitizer"] = self._get_input_sanitizer()
        self._services["rate_limiter"] = self._get_rate_limiter()

        # 监控代理
        if hasattr(self, "sla_alert_mgr"):
            self._services["alert_manager"] = self.sla_alert_mgr

        logger.info(f"组件注册表: {len(self._services)} 个服务")

    def _get_input_sanitizer(self):
        """v6.1: 返回输入净化模块的引用代理。"""
        import types
        sanitizer = types.ModuleType("input_sanitizer")
        try:
            from api.utils import sanitize_input
            sanitizer.sanitize = sanitize_input
        except ImportError:
            sanitizer.sanitize = lambda x, **kw: x
        return sanitizer

    def _get_rate_limiter(self):
        """v6.1: 返回限流器配置引用。"""
        import types
        limiter = types.ModuleType("rate_limiter")
        try:
            from core.config import RATE_LIMIT_MAX, RATE_LIMIT_WINDOW
            limiter.max_requests = RATE_LIMIT_MAX
            limiter.window_seconds = RATE_LIMIT_WINDOW
        except ImportError:
            limiter.max_requests = 60
            limiter.window_seconds = 60
        return limiter

    async def close(self):
        """P1-3: 优雅关闭，按依赖逆序释放资源"""
        if not self._initialized:
            return

        logger.info("ServiceContainer 开始关闭...")

        # 0. v6.1: 停止缓存清理任务
        if hasattr(self, "_cache_cleanup_task") and self._cache_cleanup_task:
            self._cache_cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._cache_cleanup_task
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

        # 1.5. 关闭 LangGraph checkpoint 后端（PostgreSQL 连接池）
        await self._close_checkpointer()

        # 1.6. 关闭 Embedding HTTP 连接池（rag.api_embedding 模块级 AsyncClient 注册表）
        #
        # 容器在 _init_rag 里创建了 ApiEmbedding（self.embedding_model），但 embedding 的
        # httpx.AsyncClient 是 rag.api_embedding 的**模块级**注册表（按归属 event loop 分区，
        # 见 #44），并不是某个 ApiEmbedding 实例的私有属性 —— 所以实例上没有任何可关的句柄，
        # 只能通过模块函数释放。QdrantKnowledgeBase 自建的 ApiEmbedding 实例同理受益：注册表
        # 是全局的，关一次即覆盖全部实例。
        #
        # close_async_client() 自身幂等、且只 aclose 归属当前 loop 的 client（绝不在别的 loop
        # 上强行 aclose），所以这里不需要额外的跨 loop 保护。try/except 的目的是让 embedding
        # 清理失败不阻断后面的 ERP 关闭与状态重置 —— 与上面 LLM/checkpointer 同一套约定。
        try:
            from rag.api_embedding import close_async_client

            report = await close_async_client()
            logger.info(
                "  ✅ Embedding HTTP 连接池已关闭"
                f"（aclose={report.closed}，丢弃已销毁归属 {report.dropped_dead_owners} 个"
                f"，未关闭的其他存活归属 {report.foreign_live_owners} 个）"
            )
        except Exception as e:
            logger.warning(f"  ⚠️ Embedding 连接池关闭异常: {e}")

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
