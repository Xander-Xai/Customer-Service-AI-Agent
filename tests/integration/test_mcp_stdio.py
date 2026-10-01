"""真实 MCP stdio server 集成测试（官方 mcp SDK）。

通过子进程启动 ``mcp_fixture_server.py``（FastMCP, stdio），用 ``McpSdkClient``
连接，验证 discover / schema 归一化 / invoke / 注册进 ToolRegistry。
未安装 mcp SDK 时 skip。
"""

from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("mcp")

from tools.mcp_adapter import (  # noqa: E402
    MCPServerConfig,
    MCPToolAdapter,
    register_mcp_tools,
)
from tools.tool_registry import ToolRegistry  # noqa: E402

SERVER_SCRIPT = os.path.join(os.path.dirname(__file__), "mcp_fixture_server.py")


@pytest.mark.integration
async def test_mcp_stdio_discovery_and_invoke():
    config = MCPServerConfig(
        name="catalog",
        transport="stdio",
        command=sys.executable,
        args=(SERVER_SCRIPT,),
        allowed_tools=("get_product_info", "query_order_status"),
        timeout_seconds=20.0,
        max_payload_bytes=32768,
        risk_level="read",
    )
    adapter = MCPToolAdapter(config)  # 内部构建 McpSdkClient
    try:
        specs = await adapter.discover_tools()
        names = {s["mcp_name"] for s in specs}
        assert names == {"get_product_info", "query_order_status"}
        assert all(s["input_schema"]["type"] == "object" for s in specs)

        registry = ToolRegistry()
        registered = await register_mcp_tools(registry, adapter)
        assert set(registered) == {
            "mcp__catalog__get_product_info",
            "mcp__catalog__query_order_status",
        }

        product = await registry.execute_raw(
            "mcp__catalog__get_product_info", {"keyword": "精华"}
        )
        assert "精华" in str(product)

        order = await registry.execute_raw(
            "mcp__catalog__query_order_status", {"order_id": "ORD-1"}
        )
        assert "shipped" in str(order)
    finally:
        await adapter.close()
