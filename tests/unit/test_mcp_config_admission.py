"""Reject ambiguous MCP server configuration before opening a transport."""

import json

import pytest

from tools.mcp_adapter import load_mcp_server_configs


def _server(**overrides):
    return {
        "name": "catalog",
        "command": "trusted-local-server",
        "risk_level": "low",
        "allowed_tools": ["lookup"],
        **overrides,
    }


@pytest.mark.parametrize(
    "invalid",
    [
        {"allowed_tools": "lookup"},
        {"allowed_tools": {"lookup": True}},
        {"allowed_tools": [1]},
        {"allowed_tools": [None]},
        {"allowed_tools": [" "]},
        {"args": "--unsafe"},
        {"args": {"--mode": "read"}},
        {"args": [123]},
        {"args": ["bad\x00arg"]},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": "inf"},
        {"timeout_seconds": True},
        {"max_payload_bytes": True},
        {"max_payload_bytes": float("inf")},
    ],
)
def test_malformed_entry_is_rejected_before_connection(invalid):
    raw = json.dumps([_server(**invalid), _server(name="healthy")])
    configs = load_mcp_server_configs(raw)
    assert [config.name for config in configs] == ["healthy"]


def test_empty_allowlist_remains_fail_closed():
    (config,) = load_mcp_server_configs(json.dumps([_server(allowed_tools=[], args=[""])]))
    assert config.allowed_tools == ()
    assert config.args == ("",)
