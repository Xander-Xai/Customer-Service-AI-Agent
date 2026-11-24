from tools.mcp_adapter import normalize_input_schema
from tools.readonly_mcp_faq import FAQServer


def test_faq_server_requires_handshake_and_rejects_unknown_actions():
    server = FAQServer()

    def rpc(method, params=None):
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return server.dispatch(request)

    assert "error" in rpc("tools/list")
    assert "result" in rpc("initialize", {"protocolVersion": "2025-06-18"})
    assert server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = rpc("tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["lookup_demo_faq"]
    assert normalize_input_schema(tools[0]["inputSchema"]) == tools[0]["inputSchema"]
    assert "error" in rpc("tools/call", {"name": "refund", "arguments": {}})
    arguments = {"topic": "demo_shipping"}
    result = rpc("tools/call", {"name": "lookup_demo_faq", "arguments": arguments})
    expected = "Fictional example: shipping takes 3 demo days."
    assert result["result"]["content"][0]["text"] == expected
    for args in (
        {"topic": ["demo_shipping"]},
        {"topic": "unknown"},
        {"topic": "demo_shipping", "x": 1},
    ):
        rejected = rpc("tools/call", {"name": "lookup_demo_faq", "arguments": args})
        assert rejected["result"]["isError"]
