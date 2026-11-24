"""MCP normalized output budget, including error payloads and Unicode."""

import asyncio

import pytest

from tools.mcp_adapter import MCPServerConfig, MCPToolAdapter, MCPToolExecutionError


class Client:
    def __init__(self, result):
        self.result = result

    async def call_tool(self, name, arguments):
        return self.result


def invoke(result):
    adapter = MCPToolAdapter(
        MCPServerConfig(name="test", allowed_tools=("read",)), client=Client(result)
    )
    return asyncio.run(adapter.invoke("read", {}))


def test_small_structured_output():
    assert invoke({"isError": False, "structuredContent": {"value": "正常"}}) == {"value": "正常"}


@pytest.mark.parametrize(
    "result",
    [
        {"isError": False, "structuredContent": {"value": "中" * 12000}},
        {"isError": False, "content": [{"text": "x" * 33000}]},
        {"isError": True, "content": [{"text": "secret" * 6000}]},
    ],
)
def test_large_output_is_rejected_without_echo(result):
    with pytest.raises(MCPToolExecutionError) as error:
        invoke(result)
    assert error.value.reason == "result_too_large"
    assert "secret" not in str(error.value)


def test_non_finite_output_is_rejected():
    with pytest.raises(MCPToolExecutionError) as error:
        invoke({"isError": False, "structuredContent": {"value": float("nan")}})
    assert error.value.reason == "result_not_serializable"


@pytest.mark.parametrize(
    "result",
    [
        {"isError": 0, "content": []},
        {"isError": "false", "content": []},
        {"isError": None, "content": []},
        {"isError": False, "content": "invalid"},
        {"isError": True, "content": 42},
    ],
)
def test_malformed_sdk_result_fails_closed(result):
    with pytest.raises(MCPToolExecutionError) as error:
        invoke(result)
    assert error.value.reason == "unreadable_result"
