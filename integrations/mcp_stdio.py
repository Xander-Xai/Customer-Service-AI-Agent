"""Minimal MCP stdio initialization transport shared by standalone fixtures.

This module deliberately has no application-package imports: a trusted
read-only MCP child must not import the API, load its secrets, or initialize
the Agent Runtime just to answer protocol handshakes.
"""

from __future__ import annotations

from typing import Any

VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


class MCPStdioServer:
    server_name = "customer-service-readonly"
    server_version = "1.0.0"

    def __init__(self) -> None:
        self.initialized = False
        self.ready = False

    def dispatch(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid request"},
            }
        method = request.get("method")
        params = request.get("params", {})
        if "id" not in request:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if not isinstance(params, dict):
            return {**response, "error": {"code": -32602, "message": "Invalid parameters"}}
        if method == "initialize":
            if self.initialized:
                return {**response, "error": {"code": -32600, "message": "Already initialized"}}
            version = params.get("protocolVersion")
            if not isinstance(version, str):
                return {
                    **response,
                    "error": {"code": -32602, "message": "Protocol version required"},
                }
            self.initialized = True
            return {
                **response,
                "result": {
                    "protocolVersion": version if version in VERSIONS else "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.server_name, "version": self.server_version},
                },
            }
        if method == "ping":
            return {**response, "result": {}}
        if not self.ready:
            return {**response, "error": {"code": -32600, "message": "Initialization required"}}
        return {**response, "error": {"code": -32601, "message": "Method not found"}}
