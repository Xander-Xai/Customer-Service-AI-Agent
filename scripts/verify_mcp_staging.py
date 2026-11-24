"""Explicit, operator-owned read-only MCP staging acceptance (no default endpoint)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.hitl.risk import RiskLevel
from tools.mcp_adapter import (
    MCPToolAdapter,
    MCPUnauthorizedToolError,
    load_mcp_server_configs,
    qualified_tool_name,
    register_mcp_tools,
)
from tools.tool_registry import SOURCE_MCP, ToolRegistry


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def parse_manifest(manifest: Any):
    """Accept one explicitly approved read-only tool and independent expected result."""
    if not isinstance(manifest, dict) or set(manifest) != {
        "server",
        "tool",
        "arguments",
        "expected_result",
    }:
        raise ValueError("Invalid manifest fields")
    server, tool, arguments = manifest["server"], manifest["tool"], manifest["arguments"]
    if not isinstance(server, dict) or not isinstance(tool, str) or not tool.strip():
        raise ValueError("Invalid server or tool")
    if server.get("risk_level") != "low" or server.get("allowed_tools") != [tool]:
        raise ValueError("Exactly one independently reviewed read-only tool required")
    if not isinstance(arguments, dict) or manifest["expected_result"] is None:
        raise ValueError("Arguments and independent expected result required")
    configs = load_mcp_server_configs(_canonical([server]))
    if len(configs) != 1 or not configs[0].enabled or configs[0].risk_level is not RiskLevel.LOW:
        raise ValueError("Invalid server configuration")
    config = configs[0]
    if config.transport == "stdio" and not config.command:
        raise ValueError("Missing trusted executable")
    if config.transport == "sse":
        url = urlsplit(config.url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Use an approved SSE endpoint without embedded credentials")
    qualified_tool_name(config.name, tool)
    _canonical(arguments)
    _canonical(manifest["expected_result"])
    return config, tool, arguments, manifest["expected_result"]


async def run_probe(manifest: Any) -> dict[str, Any]:
    config, tool, arguments, expected = parse_manifest(manifest)
    adapter = MCPToolAdapter(config)
    registry = ToolRegistry()
    name = qualified_tool_name(config.name, tool)
    report = {
        "schema": "mcp-staging-readonly-v1",
        "transport": config.transport,
        "discovery_and_registry": False,
        "unlisted_tool_blocked": False,
        "execution_receipt_ok": False,
        "independent_expected_result_matched": False,
        "overall_status": "FAIL",
        "production_auth_verified": False,
        "write_operations_verified": False,
        "server_provenance": "OPERATOR_DECLARED_NOT_INDEPENDENTLY_ATTESTED",
    }
    try:
        registered = await register_mcp_tools(registry, adapter)
        report["discovery_and_registry"] = (
            registered == [name]
            and registry.source_for(name) == SOURCE_MCP
            and not registry.is_side_effect(name)
            and registry.risk_level_for(name) == "low"
            and any(
                item.get("function", {}).get("name") == name for item in registry.get_openai_tools()
            )
        )
        if not report["discovery_and_registry"]:
            return report
        denied = "not_allowed_probe" if tool != "not_allowed_probe" else "not_allowed_probe_2"
        try:
            await adapter.invoke(denied, {})
        except MCPUnauthorizedToolError:
            report["unlisted_tool_blocked"] = True
        if not report["unlisted_tool_blocked"]:
            return report
        receipt = await registry.execute_raw(name, arguments, return_receipt=True)
        report["execution_receipt_ok"] = receipt.ok is True
        report["independent_expected_result_matched"] = report[
            "execution_receipt_ok"
        ] and _canonical(receipt.value) == _canonical(expected)
    except Exception:
        # Do not put remote errors, endpoint, arguments, or tool result in the artifact.
        report["failure_reason"] = "probe_execution_failed"
    finally:
        await adapter.close()
    if all(
        report[key]
        for key in (
            "discovery_and_registry",
            "unlisted_tool_blocked",
            "execution_receipt_ok",
            "independent_expected_result_matched",
        )
    ):
        report["overall_status"] = "PASS"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--permit-staging-mcp", action="store_true")
    args = parser.parse_args(argv)
    if not args.permit_staging_mcp:
        parser.error("Explicit staging connection permission required")
    # Keep the published report free of application/SDK log content.
    logging.disable(logging.CRITICAL)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        report = asyncio.run(run_probe(manifest))
    except Exception:
        report = {
            "schema": "mcp-staging-readonly-v1",
            "overall_status": "FAIL",
            "failure_reason": "invalid_or_unavailable",
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
