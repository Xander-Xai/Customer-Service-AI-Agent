"""
依赖注入容器（v4.5）
管理所有系统组件的生命周期和依赖关系。

替代 multi_agent_customer_service.py 中的模块级全局变量，
提供显式的依赖管理，便于测试、替换和生命周期控制。

v4.5: 图构建统一委托给 build_graph(container)，
消除容器内 _build_graph() 的重复图拓扑定义。

Usage:
    # 推荐方式：通过容器初始化
    container = ServiceContainer()
    await container.initialize()
    # container.graph_app 在 initialize() 中自动构建
"""
import asyncio
from typing import Dict, Any

from logger import get_logger

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
        from core.message_bus import MessageBus
        from core.shared_blackboard import SharedBlackboard
        from core.monitoring import MetricsCollector, CircuitBreaker, SLAAlertManager

        from config import (
            CACHE_L1_MAX, CACHE_L2_MAX, CACHE_TTL,
            SESSION_STORAGE_BACKEND, SESSION_WINDOW_SIZE, REDIS_URL,
        )

        self.bus = MessageBus()
        self.bb = SharedBlackboard()
        self.metrics = MetricsCollector()
        self.circuit_breaker = CircuitBreaker()
        self.sla_alert_mgr = SLAAlertManager(bus=self.bus)

        from cache.response_cache import ResponseCache
        self.cache = ResponseCache(
            l1_max=CACHE_L1_MAX, l2_max=CACHE_L2_MAX, default_ttl=CACHE_TTL,
        )

        # Session: 可选 Redis 持久化
        from session_manager import EnhancedSessionManager, default_session_manager
        self.session_mgr = default_session_manager
        if SESSION_STORAGE_BACKEND == "redis":
            try:
                self.session_mgr = EnhancedSessionManager(
                    storage_backend="redis",
                    window_size=SESSION_WINDOW_SIZE,
                    url=REDIS_URL,
                )
                self.cache._init_redis()
            except Exception:
                logger.warning("Redis 初始化失败，回退到内存模式")

        # ===== 延迟初始化组件（initialize() 中设置）=====
        self.llm: Any = None
        self.vision_llm: Any = None  # v5.1: Vision LLM（多模态模型）

        # ERP
        self.erp: Any = None

        # Agents
        self.agents_dict: Dict[str, Any] = {}
        self.response_agent: Any = None

        # Session & Router
        self.router: Any = None

        # Orchestrator（依赖 bus + bb，已在上面创建）
        from collaboration.orchestrator import CollaborationOrchestrator
        self.orchestrator = CollaborationOrchestrator(self.bus, self.bb)

        # RAG & Tools
        self.knowledge_base: Any = None
        self.tool_registry: Any = None

        # v4.1: LangGraph 应用实例
        self.graph_app: Any = None

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

            # 1.5. v5.1: Vision LLM（多模态模型，仅在启用时初始化）
            await self._init_vision_llm()

            # 2. ERP
            if self.erp is None:
                from erp.factory import create_erp_adapter
                self.erp = create_erp_adapter()

            # 3. RAG + Tools
            await self._init_rag_and_tools()

            # 4. Agents
            await self._init_agents()

            # 5. Router
            await self._init_router()

            # 6. 构建 LangGraph 应用
            self._build_graph()

            self._initialized = True
            logger.info(f"ServiceContainer 初始化完成 "
                        f"({len(self.agents_dict)} agents)")

    def _build_graph(self):
        """构建 LangGraph 工作流图（委托给 multi_agent_customer_service.build_graph）"""
        from multi_agent_customer_service import build_graph
        self.graph_app = build_graph(self)
        logger.info("[Container] LangGraph 构建完成")

    # ===== 内部初始化方法 =====

    async def _init_llm(self):
        """初始化 LLM 客户端（v4.1: 智能降级 - API Key 无效时自动切换到规则引擎）"""
        if self.llm is not None:
            return
        from llm.client import OpenAICompatibleClient
        from config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, DEV_MODE

        # v4.1: 检查 API Key 是否有效
        api_key_valid = OPENAI_API_KEY and not OPENAI_API_KEY.startswith("your_")

        if not api_key_valid and DEV_MODE:
            # 开发模式：API Key 无效时自动降级到规则引擎
            try:
                from llm.rule_based_llm import RuleBasedLLM
                logger.warning("⚠️ DeepSeek API Key 未配置，自动切换到规则引擎模式（开发降级）")
                logger.warning("💡 配置真实的 API Key：编辑 .env.dev 文件第 7 行")
                self.llm = RuleBasedLLM()
            except ImportError:
                logger.error("❌ 规则引擎模块不可用，请配置 API Key")
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

    async def _init_vision_llm(self):
        """v5.1: 初始化 Vision LLM 客户端（仅在 MULTIMODAL_ENABLED 时）"""
        from config import MULTIMODAL_ENABLED, VISION_MODEL, VISION_BASE_URL, VISION_API_KEY
        from config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL

        if not MULTIMODAL_ENABLED:
            return

        # 从环境变量获取，留空则复用默认 LLM 配置
        vision_model = VISION_MODEL or OPENAI_MODEL
        vision_base_url = VISION_BASE_URL or OPENAI_BASE_URL
        vision_api_key = VISION_API_KEY or OPENAI_API_KEY

        # 如果 Vision 模型与默认模型相同，复用同一个客户端
        if (vision_model == OPENAI_MODEL
                and vision_base_url == OPENAI_BASE_URL
                and vision_api_key == OPENAI_API_KEY):
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

    async def _init_rag_and_tools(self):
        """初始化 RAG 知识库 + 工具注册"""
        if self.knowledge_base is None:
            from rag.knowledge_base import CosmeticsKnowledgeBase
            from rag.seed_data import (
                seed_product_knowledge, seed_faq, seed_tech_support, seed_complaint_knowledge,
                seed_supplementary_data,
            )
            from config import CLIP_ENABLED
            self.knowledge_base = CosmeticsKnowledgeBase(clip_enabled=CLIP_ENABLED)
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

    async def _init_agents(self):
        """初始化所有 Agent 实例"""
        if self.agents_dict:
            return

        from agents import (
            ProductAgent, TechAgent, BillingAgent, ComplaintAgent,
            GeneralAgent, ResponseAgent, ReActAgent,
        )
        from config import ERP_MODE

        agent_classes = {
            "product_agent": ProductAgent,
            "tech_agent": TechAgent,
            "billing_agent": BillingAgent,
            "complaint_agent": ComplaintAgent,
            "general_agent": GeneralAgent,
        }

        for name, cls in agent_classes.items():
            agent = cls()
            agent.set_llm(self.llm)
            agent.set_session_manager(self.session_mgr)
            agent.set_bus(self.bus)
            agent.set_blackboard(self.bb)
            agent.set_erp(self.erp)
            if self.vision_llm:  # v5.1: 注入 Vision LLM
                agent.set_vision_llm(self.vision_llm)
            self.agents_dict[name] = agent

        # RAG 注入到需要检索的 Agent
        for name in ("product_agent", "tech_agent", "complaint_agent"):
            if name in self.agents_dict:
                self.agents_dict[name].set_knowledge_base(self.knowledge_base)

        # ReAct 推理 Agent
        react_agent = ReActAgent()
        react_agent.set_llm(self.llm)
        react_agent.set_session_manager(self.session_mgr)
        react_agent.set_bus(self.bus)
        react_agent.set_blackboard(self.bb)
        react_agent.set_erp(self.erp)
        react_agent.set_knowledge_base(self.knowledge_base)
        react_agent.set_tool_registry(self.tool_registry)
        if self.vision_llm:  # v5.1: 注入 Vision LLM
            react_agent.set_vision_llm(self.vision_llm)
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
            f"初始化 {len(self.agents_dict)} 个 Agent "
            f"(含 ReActAgent) 完成 (ERP_MODE={ERP_MODE})"
        )

    async def _init_router(self):
        """初始化查询路由器"""
        if self.router is not None:
            return
        from router.query_router import QueryRouter
        from config import ROUTING_COMPLEXITY_THRESHOLD
        self.router = QueryRouter(
            llm=self.llm,
            complexity_threshold=ROUTING_COMPLEXITY_THRESHOLD,
        )

    async def close(self):
        """P1-3: 优雅关闭，按依赖逆序释放资源"""
        if not self._initialized:
            return

        logger.info("ServiceContainer 开始关闭...")

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
