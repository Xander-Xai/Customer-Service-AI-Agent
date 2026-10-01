"""MCP Adapter 单元测试（fake client，无需真实 server）。

覆盖：tool discovery / schema conversion / native+MCP coexist / timeout /
server unavailable / unauthorized tool / invalid schema / payload limit /
write-risk skip / metrics / config fail-closed。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tools.mcp_adapter import (
    MCPError,
    MCPInvalidSchemaError,
    MCPServerConfig,
    MCPTimeoutError,
    MCPToolAdapter,
    MCPUnauthorizedToolError,
    MCPUnavailableError,
    load_mcp_server_configs,
    normalize_input_schema,
    qualified_tool_name,
    register_mcp_tools,
)
from tools.tool_registry import ToolRegistry


class FakeMCPClient:
    def __init__(self, tools, *, delay=0.0, call_error=None, list_error=None):
        self.tools = tools
        self.delay = delay
        self.call_error = call_error
        self.list_error = list_error
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        if self.list_error:
            raise self.list_error
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.call_error:
            raise self.call_error
        return SimpleNamespace(
            isError=False,
            structuredContent={"ok": True, "echo": arguments},
            content=[SimpleNamespace(type="text", text="ok")],
        )

    async def close(self):
        return None


def _tool(name, description="d", schema=None, risk=None):
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema if schema is not None else {"type": "object", "properties": {}},
    )


def _config(**overrides):
    base = dict(
        name="catalog",
        allowed_tools=("get_product_info", "query_order_status"),
        timeout_seconds=1.0,
        max_payload_bytes=1024,
    )
    base.update(overrides)
    return MCPServerConfig(**base)


# ---------------------------------------------------------------------------
# discovery / schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_tool_discovery_filters_allowlist():
    client = FakeMCPClient(
        [_tool("get_product_info"), _tool("query_order_status"), _tool("danger")]
    )
    adapter = MCPToolAdapter(_config(), client=client)
    specs = await adapter.discover_tools()
    assert [s["mcp_name"] for s in specs] == ["get_product_info", "query_order_status"]
    assert all(s["name"].startswith("mcp__catalog__") for s in specs)
    assert all(s["source"] == "mcp" and s["risk_level"] == "read" for s in specs)


@pytest.mark.unit
def test_schema_conversion_normalizes_and_rejects_invalid():
    schema = normalize_input_schema({"type": "object", "properties": {"keyword": {"type": "string"}}})
    assert schema["type"] == "object"
    assert "keyword" in schema["properties"]
    # 缺省补齐
    assert normalize_input_schema(None) == {"type": "object", "properties": {}}
    assert normalize_input_schema({"properties": {}})["type"] == "object"
    # 非法 fail closed
    with pytest.raises(MCPInvalidSchemaError):
        normalize_input_schema({"type": "array"})
    with pytest.raises(MCPInvalidSchemaError):
        normalize_input_schema("not-a-schema")


@pytest.mark.unit
async def test_invalid_schema_tool_is_skipped():
    client = FakeMCPClient(
        [_tool("get_product_info", schema={"type": "array"}), _tool("query_order_status")]
    )
    adapter = MCPToolAdapter(_config(), client=client)
    specs = await adapter.discover_tools()
    assert [s["mcp_name"] for s in specs] == ["query_order_status"]


@pytest.mark.unit
def test_qualified_tool_name_is_fc_safe_and_bounded():
    assert qualified_tool_name("catalog", "get_product_info") == "mcp__catalog__get_product_info"
    long = qualified_tool_name("s" * 80, "t" * 20)
    assert len(long) <= 64
    assert long.startswith("mcp__")


# ---------------------------------------------------------------------------
# invoke / coexist
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_native_and_mcp_coexist():
    registry = ToolRegistry()

    async def native_handler(args):
        return {"native": True}

    registry.register(
        name="native_echo", description="native", parameters={"type": "object", "properties": {}},
        handler=native_handler,
    )
    adapter = MCPToolAdapter(_config(), client=FakeMCPClient([_tool("get_product_info")]))
    names = await register_mcp_tools(registry, adapter)

    assert "native_echo" in registry.list_tools()
    assert names == ["mcp__catalog__get_product_info"]
    # 统一 OpenAI FC schema（Agent 不关心来源）
    fc_names = [t["function"]["name"] for t in registry.get_openai_tools()]
    assert "native_echo" in fc_names and "mcp__catalog__get_product_info" in fc_names

    native_out = await registry.execute_raw("native_echo", {})
    mcp_out = await registry.execute_raw("mcp__catalog__get_product_info", {"keyword": "x"})
    assert native_out == {"native": True}
    assert mcp_out["ok"] is True


@pytest.mark.unit
async def test_invoke_returns_structured_result():
    adapter = MCPToolAdapter(_config(), client=FakeMCPClient([_tool("get_product_info")]))
    result = await adapter.invoke("get_product_info", {"keyword": "精华"})
    assert result == {"ok": True, "echo": {"keyword": "精华"}}


@pytest.mark.unit
async def test_invoke_maps_is_error_result():
    class ErrClient(FakeMCPClient):
        async def call_tool(self, name, arguments):
            return SimpleNamespace(isError=True, content=[SimpleNamespace(type="text", text="boom")])

    adapter = MCPToolAdapter(_config(), client=ErrClient([]))
    with pytest.raises(MCPError, match="boom"):
        await adapter.invoke("get_product_info", {})


# ---------------------------------------------------------------------------
# 安全 / 失败行为
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_unauthorized_tool_rejected():
    adapter = MCPToolAdapter(_config(), client=FakeMCPClient([_tool("get_product_info")]))
    with pytest.raises(MCPUnauthorizedToolError):
        await adapter.invoke("danger", {})


@pytest.mark.unit
async def test_timeout_maps_to_mcp_timeout():
    adapter = MCPToolAdapter(
        _config(timeout_seconds=0.05),
        client=FakeMCPClient([_tool("get_product_info")], delay=0.3),
    )
    with pytest.raises(MCPTimeoutError):
        await adapter.invoke("get_product_info", {})


@pytest.mark.unit
async def test_server_unavailable_maps_to_unavailable():
    adapter = MCPToolAdapter(
        _config(), client=FakeMCPClient([], list_error=ConnectionError("refused"))
    )
    with pytest.raises(MCPUnavailableError):
        await adapter.discover_tools()

    adapter2 = MCPToolAdapter(
        _config(),
        client=FakeMCPClient([_tool("get_product_info")], call_error=ConnectionError("refused")),
    )
    with pytest.raises(MCPUnavailableError):
        await adapter2.invoke("get_product_info", {})


@pytest.mark.unit
async def test_payload_too_large_rejected():
    adapter = MCPToolAdapter(
        _config(max_payload_bytes=16), client=FakeMCPClient([_tool("get_product_info")])
    )
    with pytest.raises(MCPError):
        await adapter.invoke("get_product_info", {"keyword": "x" * 100})


@pytest.mark.unit
async def test_write_risk_mcp_tool_not_registered():
    adapter = MCPToolAdapter(
        _config(risk_level="write", allowed_tools=("get_product_info",)),
        client=FakeMCPClient([_tool("get_product_info")]),
    )
    registry = ToolRegistry()
    names = await register_mcp_tools(registry, adapter)
    assert names == []
    assert registry.list_tools() == []


# ---------------------------------------------------------------------------
# 配置 / 指标
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_mcp_server_configs_fail_closed():
    assert load_mcp_server_configs("") == []
    assert load_mcp_server_configs("{not json") == []
    assert load_mcp_server_configs('{"not":"a list"}') == []
    # 非法 transport 跳过
    assert load_mcp_server_configs('[{"name":"s","transport":"carrier-pigeon"}]') == []
    cfgs = load_mcp_server_configs(
        '[{"name":"catalog","transport":"stdio","command":"python3",'
        '"allowed_tools":["get_product_info"],"risk_level":"read"}]'
    )
    assert len(cfgs) == 1
    assert cfgs[0].allowed_tools == ("get_product_info",)


@pytest.mark.unit
def test_mcp_metrics_registered():
    from core import monitoring

    for name in ("mcp_tool_call_total", "mcp_tool_error_total", "mcp_tool_duration_seconds"):
        assert getattr(monitoring, name, None) is not None, f"missing metric: {name}"


@pytest.mark.unit
async def test_container_mcp_disabled_is_noop(monkeypatch):
    import core.config as cfg

    monkeypatch.setattr(cfg, "MCP_ENABLED", False)
    from core.container import ServiceContainer

    container = ServiceContainer()
    container.tool_registry = ToolRegistry()
    await container._init_mcp_tools()
    assert container.mcp_adapters == []
    assert container.tool_registry.list_tools() == []
