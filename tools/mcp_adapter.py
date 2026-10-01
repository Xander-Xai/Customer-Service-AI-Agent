"""MCP（Model Context Protocol）客户端适配器。

设计取舍：
  - **保留 native Function Calling**：native 工具零网络、低延迟、可注入
    side-effect 幂等（``ToolRegistry`` 已有能力），是系统内建能力。
  - **MCP 作为标准化外部工具接口**：跨进程/跨语言/第三方工具通过 MCP server
    暴露，由本适配器 discover + 归一化 + invoke 后注册进同一个 ``ToolRegistry``。
  - **不是用 MCP 替代所有工具**：Agent / ReAct / FC 只看到统一的 ToolDefinition
    （name/description/input_schema/invoke/risk_level/timeout），不关心来源。

安全（fail closed）：
  - 只允许 ``MCP_SERVERS`` allowlist 中的 server / tool；
  - 每个 server 限制 transport、timeout、payload 大小；
  - 第一版只接只读工具；写操作 MCP 工具默认不注册（需显式风险评审 + 幂等）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from core.logger import get_logger
from core.tool_result_cache import ToolCachePolicy

logger = get_logger("tools.mcp")

RISK_READ = "read"
RISK_WRITE = "write"

_SUPPORTED_TRANSPORTS = ("stdio", "sse")
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


class MCPError(RuntimeError):
    """MCP 调用错误基类。"""


class MCPTimeoutError(MCPError):
    """MCP 调用超时。"""


class MCPUnauthorizedToolError(MCPError):
    """工具不在 allowlist 中（fail closed）。"""


class MCPUnavailableError(MCPError):
    """MCP server 不可用 / 连接失败。"""


class MCPInvalidSchemaError(MCPError):
    """MCP tool schema 非法（无法归一化）。"""


@dataclass(frozen=True)
class MCPServerConfig:
    """MCP server allowlist 配置（来自 MCP_SERVERS JSON）。"""

    name: str
    transport: str = "stdio"  # stdio | sse
    url: str = ""  # sse
    command: str = ""  # stdio
    args: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()  # 空 = 不允许任何工具（fail closed）
    timeout_seconds: float = 15.0
    max_payload_bytes: int = 32768
    enabled: bool = True
    risk_level: str = RISK_READ


class MCPClientProtocol(Protocol):
    """最小 MCP client 接口（真实 SDK / fake 均可实现）。"""

    async def list_tools(self) -> list[Any]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...
    async def close(self) -> None: ...


def qualified_tool_name(server: str, tool: str, *, max_len: int = 64) -> str:
    """把 MCP 工具名命名空间化，避免与 native 工具冲突（FC 名称 <=64 字符）。"""
    raw = f"mcp__{server}__{tool}"
    safe = _NAME_RE.sub("_", raw)
    if len(safe) <= max_len:
        return safe
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe[: max_len - 9]}_{digest}"


def normalize_input_schema(raw: Any) -> dict[str, Any]:
    """把 MCP ``inputSchema`` 归一化为 JSON Schema object。

    非法 schema fail closed（抛 ``MCPInvalidSchemaError``），不做静默猜测。
    """
    if raw is None:
        return {"type": "object", "properties": {}}
    if not isinstance(raw, dict):
        raise MCPInvalidSchemaError("inputSchema 不是 JSON object")
    schema = dict(raw)
    if schema.get("type") not in (None, "object"):
        raise MCPInvalidSchemaError(f"inputSchema.type 非法: {schema.get('type')!r}")
    schema["type"] = "object"
    props = schema.get("properties")
    if props is None:
        schema["properties"] = {}
    elif not isinstance(props, dict):
        raise MCPInvalidSchemaError("inputSchema.properties 不是 object")
    return schema


def load_mcp_server_configs(
    raw: str,
    *,
    default_timeout: float = 15.0,
    default_max_payload: int = 32768,
) -> list[MCPServerConfig]:
    """解析 MCP_SERVERS JSON allowlist。非法输入 fail closed（返回 []）。"""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("MCP_SERVERS JSON 解析失败，禁用 MCP: %s", type(e).__name__)
        return []
    if not isinstance(data, list):
        logger.warning("MCP_SERVERS 必须是 JSON 数组，禁用 MCP")
        return []

    configs: list[MCPServerConfig] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        transport = str(item.get("transport", "stdio")).strip().lower()
        if transport not in _SUPPORTED_TRANSPORTS:
            logger.warning("MCP server %s transport 不支持: %s", item.get("name"), transport)
            continue
        try:
            configs.append(
                MCPServerConfig(
                    name=str(item["name"]),
                    transport=transport,
                    url=str(item.get("url", "")),
                    command=str(item.get("command", "")),
                    args=tuple(str(a) for a in item.get("args", []) or []),
                    allowed_tools=tuple(str(t) for t in item.get("allowed_tools", []) or []),
                    timeout_seconds=float(item.get("timeout_seconds", default_timeout)),
                    max_payload_bytes=int(item.get("max_payload_bytes", default_max_payload)),
                    enabled=bool(item.get("enabled", True)),
                    risk_level=str(item.get("risk_level", RISK_READ)),
                )
            )
        except (TypeError, ValueError) as e:
            logger.warning("MCP server 配置非法，跳过: %s", type(e).__name__)
    return configs


def _inc(name: str, **labels: Any) -> None:
    try:
        from core import monitoring

        metric = getattr(monitoring, name, None)
        if metric is None:
            return
        if labels:
            metric.labels(**labels).inc()
        else:
            metric.inc()
    except Exception:
        pass


def _observe(name: str, value: float, **labels: Any) -> None:
    try:
        from core import monitoring

        metric = getattr(monitoring, name, None)
        if metric is None:
            return
        if labels:
            metric.labels(**labels).observe(value)
        else:
            metric.observe(value)
    except Exception:
        pass


class MCPToolAdapter:
    """单个 MCP server 的适配器：discover / normalize / invoke。"""

    def __init__(self, config: MCPServerConfig, client: MCPClientProtocol | None = None):
        self.config = config
        self._client = client
        self._owns_client = client is None
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return
        if self._client is None:
            self._client = McpSdkClient(self.config)
        connect = getattr(self._client, "connect", None)
        if connect is not None:
            await connect()
        self._connected = True

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            try:
                await self._client.close()
            except Exception as e:  # pragma: no cover - best effort
                logger.debug("MCP client close 失败: %s", type(e).__name__)
        self._connected = False

    async def discover_tools(self) -> list[dict[str, Any]]:
        """发现并归一化 allowlist 内的工具。"""
        await self.connect()
        assert self._client is not None
        try:
            raw_tools = await self._client.list_tools()
        except Exception as e:
            mapped = self._map_error(e)
            _inc(
                "mcp_tool_error_total",
                server=self.config.name,
                tool="*",
                reason=type(mapped).__name__,
            )
            raise mapped from e
        allowed = set(self.config.allowed_tools)
        specs: list[dict[str, Any]] = []
        for raw in raw_tools:
            name = getattr(raw, "name", None) or (raw.get("name") if isinstance(raw, dict) else None)
            if not name:
                continue
            if name not in allowed:
                _inc("mcp_tool_error_total", server=self.config.name, tool=name, reason="not_allowed")
                logger.info("MCP tool 不在 allowlist，跳过: %s/%s", self.config.name, name)
                continue
            description = (
                getattr(raw, "description", None)
                or (raw.get("description") if isinstance(raw, dict) else "")
                or ""
            )
            raw_schema = (
                getattr(raw, "inputSchema", None)
                or (raw.get("inputSchema") if isinstance(raw, dict) else None)
            )
            try:
                schema = normalize_input_schema(raw_schema)
            except MCPInvalidSchemaError as e:
                _inc("mcp_tool_error_total", server=self.config.name, tool=name, reason="invalid_schema")
                logger.warning("MCP tool schema 非法，跳过 %s/%s: %s", self.config.name, name, e)
                continue
            specs.append(
                {
                    "name": qualified_tool_name(self.config.name, name),
                    "mcp_name": name,
                    "description": description,
                    "input_schema": schema,
                    "risk_level": self.config.risk_level,
                    "timeout": self.config.timeout_seconds,
                    "source": "mcp",
                    "server": self.config.name,
                }
            )
        return specs

    def _validate_payload(self, arguments: dict[str, Any] | None, tool_name: str) -> dict[str, Any]:
        args = dict(arguments or {})
        encoded = json.dumps(args, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > self.config.max_payload_bytes:
            _inc(
                "mcp_tool_error_total",
                server=self.config.name,
                tool=tool_name,
                reason="payload_too_large",
            )
            raise MCPError("MCP 调用 payload 超过大小上限")
        return args

    async def invoke(self, mcp_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """调用 MCP 工具：allowlist + payload 限制 + timeout + error mapping。"""
        if mcp_name not in set(self.config.allowed_tools):
            _inc("mcp_tool_error_total", server=self.config.name, tool=mcp_name, reason="not_allowed")
            raise MCPUnauthorizedToolError(
                f"MCP tool '{mcp_name}' 不在 {self.config.name} allowlist"
            )
        args = self._validate_payload(arguments, mcp_name)
        await self.connect()
        assert self._client is not None

        started = time.perf_counter()
        status = "ok"
        try:
            result = await asyncio.wait_for(
                self._client.call_tool(mcp_name, args), timeout=self.config.timeout_seconds
            )
        except asyncio.TimeoutError as e:
            status = "error"
            _inc("mcp_tool_error_total", server=self.config.name, tool=mcp_name, reason="timeout")
            raise MCPTimeoutError(
                f"MCP tool '{mcp_name}' 超时（{self.config.timeout_seconds}s）"
            ) from e
        except MCPError:
            status = "error"
            raise
        except Exception as e:
            status = "error"
            mapped = self._map_error(e)
            _inc(
                "mcp_tool_error_total",
                server=self.config.name,
                tool=mcp_name,
                reason=type(mapped).__name__,
            )
            raise mapped from e
        else:
            return self._normalize_result(result)
        finally:
            _observe(
                "mcp_tool_duration_seconds",
                time.perf_counter() - started,
                server=self.config.name,
                tool=mcp_name,
            )
            _inc("mcp_tool_call_total", server=self.config.name, tool=mcp_name, status=status)

    @staticmethod
    def _map_error(exc: Exception) -> MCPError:
        if isinstance(exc, MCPError):
            return exc
        if isinstance(exc, (ConnectionError, OSError)):
            return MCPUnavailableError(f"MCP server 不可用: {type(exc).__name__}")
        return MCPError(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _normalize_result(result: Any) -> Any:
        """把 MCP CallToolResult 归一化为结构化返回值。"""
        if result is None:
            return ""
        if not hasattr(result, "content") and not hasattr(result, "isError"):
            return result  # fake/dict/str 直接返回
        if bool(getattr(result, "isError", False)):
            text = MCPToolAdapter._extract_text(result)
            raise MCPError(text or "MCP tool 返回错误")
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured
        text = MCPToolAdapter._extract_text(result)
        return text if text else result

    @staticmethod
    def _extract_text(result: Any) -> str:
        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            if hasattr(item, "text"):
                parts.append(str(item.text))
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)


async def register_mcp_tools(
    registry: Any,
    adapter: MCPToolAdapter,
    *,
    cache_ttl_seconds: int = 300,
) -> list[str]:
    """把 MCP 工具注册进现有 ToolRegistry（统一 ToolDefinition）。

    第一版只注册只读工具（side_effect=False）；写操作 MCP 工具默认不注册。
    """
    specs = await adapter.discover_tools()
    registered: list[str] = []
    for spec in specs:
        if spec.get("risk_level") == RISK_WRITE:
            logger.warning("跳过写操作 MCP 工具（未启用幂等保护）: %s", spec["name"])
            continue
        mcp_name = spec["mcp_name"]

        async def _handler(arguments: dict[str, Any], _mcp_name: str = mcp_name, _adapter: MCPToolAdapter = adapter) -> Any:
            return await _adapter.invoke(_mcp_name, arguments)

        registry.register(
            name=spec["name"],
            description=spec["description"],
            parameters=spec["input_schema"],
            handler=_handler,
            cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=cache_ttl_seconds),
            side_effect=False,
        )
        registered.append(spec["name"])
    logger.info(
        "MCP server %s 注册 %d 个工具: %s",
        adapter.config.name,
        len(registered),
        registered,
    )
    return registered


class McpSdkClient:
    """官方 ``mcp`` SDK 的最小 client（stdio / sse）。

    生命周期在 ``connect`` / ``close`` 间保持；由 ``MCPToolAdapter`` 管理。
    真实 server 连通性属于部署验证，本地默认不连接（``MCP_ENABLED=false``）。
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._transport_cm: Any = None
        self._session_cm: Any = None
        self._session: Any = None

    async def connect(self) -> None:
        if self._session is not None:
            return
        try:
            from mcp import ClientSession
        except ImportError as e:  # pragma: no cover
            raise MCPUnavailableError("mcp SDK 未安装") from e

        transport = self.config.transport
        if transport == "stdio":
            if not self.config.command:
                raise MCPUnavailableError("stdio MCP server 缺少 command")
            from mcp.client.stdio import StdioServerParameters, stdio_client

            self._transport_cm = stdio_client(
                StdioServerParameters(command=self.config.command, args=list(self.config.args))
            )
        elif transport == "sse":
            if not self.config.url:
                raise MCPUnavailableError("sse MCP server 缺少 url")
            from mcp.client.sse import sse_client

            self._transport_cm = sse_client(
                self.config.url, timeout=min(5.0, self.config.timeout_seconds)
            )
        else:  # pragma: no cover - 配置已过滤
            raise MCPUnavailableError(f"不支持的 transport: {transport}")

        streams = await self._transport_cm.__aenter__()
        read_stream, write_stream = streams[0], streams[1]
        self._session_cm = ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=self.config.timeout_seconds),
        )
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

    async def list_tools(self) -> list[Any]:
        await self.connect()
        result = await self._session.list_tools()
        return list(getattr(result, "tools", []) or [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        await self.connect()
        return await self._session.call_tool(name, arguments or {})

    async def close(self) -> None:
        for cm in (self._session_cm, self._transport_cm):
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception as e:  # pragma: no cover - best effort
                    logger.debug("MCP SDK close 异常: %s", type(e).__name__)
        self._session_cm = None
        self._transport_cm = None
        self._session = None


def build_mcp_adapters(configs: list[MCPServerConfig]) -> list[MCPToolAdapter]:
    return [MCPToolAdapter(cfg) for cfg in configs if cfg.enabled]


__all__ = [
    "MCPError",
    "MCPTimeoutError",
    "MCPUnauthorizedToolError",
    "MCPUnavailableError",
    "MCPInvalidSchemaError",
    "MCPServerConfig",
    "MCPClientProtocol",
    "MCPToolAdapter",
    "McpSdkClient",
    "build_mcp_adapters",
    "load_mcp_server_configs",
    "normalize_input_schema",
    "qualified_tool_name",
    "register_mcp_tools",
]
