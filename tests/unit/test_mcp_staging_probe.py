"""Staging probe contract; no live external server or paid provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import verify_mcp_staging as probe
from tools.mcp_adapter import MCPUnauthorizedToolError


def manifest():
    return {
        "server": {
            "name": "staging",
            "transport": "stdio",
            "command": "approved-test-server",
            "risk_level": "low",
            "allowed_tools": ["lookup"],
        },
        "tool": "lookup",
        "arguments": {"id": "synthetic"},
        "expected_result": {"stock": 3},
    }


@pytest.mark.parametrize("risk", ["high", "medium", None])
def test_rejects_unreviewed_risk(risk):
    config = manifest()
    config["server"]["risk_level"] = risk
    with pytest.raises(ValueError):
        probe.parse_manifest(config)


def test_rejects_broad_allowlist():
    config = manifest()
    config["server"]["allowed_tools"].append("refund")
    with pytest.raises(ValueError):
        probe.parse_manifest(config)


def test_rejects_embedded_url_credentials():
    config = manifest()
    config["server"].update(transport="sse", url="https://example.invalid/sse?token=private")
    with pytest.raises(ValueError):
        probe.parse_manifest(config)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual", "expected_status"), [({"stock": 3}, "PASS"), ({"stock": 4}, "FAIL")]
)
async def test_probe_requires_independent_result_and_closes(monkeypatch, actual, expected_status):
    adapter = SimpleNamespace(
        invoke=AsyncMock(side_effect=MCPUnauthorizedToolError("blocked")),
        close=AsyncMock(),
    )
    registry = SimpleNamespace(
        source_for=lambda name: probe.SOURCE_MCP,
        is_side_effect=lambda name: False,
        risk_level_for=lambda name: "low",
        get_openai_tools=lambda: [{"function": {"name": "mcp__staging__lookup"}}],
        execute_raw=AsyncMock(return_value=SimpleNamespace(ok=True, value=actual)),
    )
    monkeypatch.setattr(probe, "MCPToolAdapter", lambda cfg: adapter)
    monkeypatch.setattr(probe, "ToolRegistry", lambda: registry)
    monkeypatch.setattr(
        probe,
        "register_mcp_tools",
        AsyncMock(return_value=["mcp__staging__lookup"]),
    )
    report = await probe.run_probe(manifest())
    assert report["overall_status"] == expected_status
    assert report["unlisted_tool_blocked"]
    assert report["production_auth_verified"] is False
    assert report["write_operations_verified"] is False
    assert "stock" not in str(report)
    adapter.close.assert_awaited_once()
    registry.execute_raw.assert_awaited_once_with(
        "mcp__staging__lookup", {"id": "synthetic"}, return_receipt=True
    )


def test_cli_requires_explicit_permission(tmp_path):
    with pytest.raises(SystemExit) as exc:
        probe.main(
            ["--manifest", str(tmp_path / "manifest.json"), "--out", str(tmp_path / "report.json")]
        )
    assert exc.value.code == 2
    assert not (tmp_path / "report.json").exists()
