"""
核心 Protocol 定义（v5.1）
用于替代 Any 类型，提供编译期类型安全。

使用 Protocol（PEP 544）而非 ABC：
- 不需要继承，只需方法签名匹配（鸭子类型）
- mypy strict 模式下可以检查依赖注入的正确性
- 面试时展示对 Python 类型系统的深入理解
"""

import inspect
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProtocol(Protocol):
    """LLM 客户端协议：所有 LLM 实现必须满足此接口。

    ``async_invoke`` 的签名是**唯一权威**的调用契约：调用方（router / agents /
    session / rag）按 ``(messages, timeout=..., tools=...)`` 调用，真实 provider
    客户端与规则引擎降级实现必须都能接住同一组参数。issue #46 就是这条契约漂移
    的实例 —— router 传 ``timeout=``，而 ``RuleBasedLLM`` 只声明了
    ``(messages, tools=None)``，于是每次 LLM 路由都抛 ``TypeError`` 并静默退化成
    规则分类。

    ``runtime_checkable`` 只校验方法**存在**，不校验签名，所以光靠
    ``issubclass(Impl, LLMProtocol)`` 抓不到这类漂移。需要签名的请用
    :func:`llm_call_contract_mismatches`。
    """

    async def async_invoke(
        self,
        messages: list[dict[str, Any]],
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """调用 LLM 生成回复。"""
        ...

    async def async_invoke_stream(
        self,
        messages: list[dict[str, Any]],
        timeout: float | None = None,
    ) -> Any:
        """流式调用 LLM（SSE）。"""
        ...


#: ``async_invoke`` 契约里除 ``self`` 外的形参名（顺序即调用方约定的顺序）。
#: 真实客户端与降级实现必须逐一对上；多一个 ``**kwargs`` 也不算数 ——
#: ``**kwargs`` 会把「调用方传错了参数」变成静默接受，正是 issue #46 想暴露的那类 bug。
LLM_CALL_CONTRACT_PARAMS: tuple[str, ...] = ("messages", "timeout", "tools")


def llm_call_contract_mismatches(impl: type) -> list[str]:
    """比对某个 LLM 实现的 ``async_invoke`` 与 :class:`LLMProtocol` 的签名差异。

    返回可读的差异描述列表；空列表表示签名一致。这是一个**纯函数**（只用
    ``inspect``，不发网络请求、不构造实例），因此可以在单测里对真实客户端和降级
    实现同时跑，且不会因为某个实现的 ``__init__`` 需要凭据而失败。

    只比对参数名与顺序，不比对注解 —— 真实客户端用 ``list`` / ``list | None``，
    协议写的是 ``list[dict[str, Any]]``，注解差异不构成调用期故障。
    """
    method = getattr(impl, "async_invoke", None)
    if method is None:
        return ["async_invoke 缺失"]

    params = tuple(inspect.signature(method).parameters)
    if params[:1] == ("self",):
        params = params[1:]

    mismatches: list[str] = []
    if params != LLM_CALL_CONTRACT_PARAMS:
        mismatches.append(f"async_invoke{params} != async_invoke{LLM_CALL_CONTRACT_PARAMS}")
    return mismatches


@runtime_checkable
class ERPProtocol(Protocol):
    """ERP 适配器协议：所有 ERP 实现必须满足此接口。"""

    async def query_product(self, keyword: str = "") -> list[dict[str, Any]]:
        """查询产品信息。"""
        ...

    async def query_inventory(self, keyword: str = "") -> list[dict[str, Any]]:
        """查询库存信息。"""
        ...

    async def query_order(self, order_id: str = "", customer_id: str = "") -> list[dict[str, Any]]:
        """查询订单信息。"""
        ...

    async def query_customer(self, customer_id: str) -> dict[str, Any] | None:
        """查询客户信息。"""
        ...

    async def get_order_owner(self, order_id: str) -> str | None:
        """P0-03: 最小归属元数据 — 仅返回订单归属 customer_id（不取完整正文）。

        缺失/不存在/查询失败时返回 None（fail closed）。
        """
        ...

    async def resolve_customer_by_user(self, user_id: str | int | None) -> str | None:
        """P0-03: 将可信 authenticated user_id 解析为 ERP customer_id。

        映射必须来自服务端权威数据；缺失时返回 None（fail closed）。
        """
        ...


@runtime_checkable
class KnowledgeBaseProtocol(Protocol):
    """RAG 知识库协议：所有知识库实现必须满足此接口。"""

    async def retrieve(self, request: Any) -> Any:
        """Unified retrieval contract; concrete types live in rag."""
        ...

    async def prefetch_reusable(self, query: str, llm: Any = None) -> dict:
        """Compute reusable query/embedding inputs, never final evidence."""
        ...

    async def query(
        self, text: str, collection: str = "", n_results: int = 3
    ) -> list[dict[str, Any]]:
        """查询知识库，返回相关文档列表。"""
        ...

    async def query_multiple(
        self, text: str, collections: list[str] = ..., n_results: int = 3
    ) -> list[dict[str, Any]]:
        """跨多个 collection 查询并合并结果。"""
        ...

    def get_collection_count(self, collection: str) -> int:
        """获取指定 collection 的文档数。"""
        ...


@runtime_checkable
class ToolRegistryProtocol(Protocol):
    """工具注册中心协议。"""

    def list_tools(self) -> list[str]:
        """列出已注册的工具名。"""
        ...

    def unregister(self, name: str) -> bool:
        """撤销注册（回滚用），返回是否真的移除过一个工具。

        MCP 工具是运行时叠加进同一个注册表的，多 server 初始化可能部分成功后失败；
        没有撤销能力就无法把注册表恢复到「本次 attempt 之前」的状态。
        """
        ...

    def get_tools_for_llm(self) -> list[dict[str, Any]]:
        """获取供 LLM Function Calling 使用的工具定义。"""
        ...

    async def execute(
        self, tool_name: str, arguments: dict[str, Any], tool_call_id: str | None = None
    ) -> Any:
        """执行指定工具。"""
        ...

    async def execute_raw(
        self, tool_name: str, arguments: dict[str, Any], tool_call_id: str | None = None
    ) -> Any:
        """执行工具并保留结构化结果（可选的 Context Engineering 路径）。"""
        ...


@runtime_checkable
class SessionManagerProtocol(Protocol):
    """会话管理器协议。"""

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """获取会话。"""
        ...

    async def add_message(self, session_id: str, message: str, role: str = "user") -> None:
        """添加消息到会话。"""
        ...

    async def get_context_messages(
        self, session_id: str, max_messages: int = 6
    ) -> list[dict[str, str]]:
        """获取上下文消息列表。"""
        ...
