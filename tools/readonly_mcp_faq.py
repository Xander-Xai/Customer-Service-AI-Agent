"""Standalone local MCP stdio server for a fictional customer FAQ catalog.

The official SDK is used by the existing client, not this server. No network,
external data, credentials, arbitrary text search, writes, or production policy.
"""

from __future__ import annotations

import json
import sys
from typing import Any

_MAX_LINE = 32768
_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
_FAQ = {
    "demo_shipping": "Fictional example: shipping takes 3 demo days.",
    "demo_returns": "Fictional example: contact the demo support desk for returns.",
    "demo_hours": "Fictional example: demo support is open on weekdays.",
}
_TOOL = {
    "name": "lookup_demo_faq",
    "description": "Look up one fictional FAQ topic. Not actual company policy.",
    "inputSchema": {
        "type": "object",
        "properties": {"topic": {"type": "string", "enum": sorted(_FAQ)}},
        "required": ["topic"],
        "additionalProperties": False,
    },
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


class FAQServer:
    """One stdio connection; requests require the MCP initialization handshake."""

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

        def error(code: int, message: str) -> dict[str, Any]:
            return {**response, "error": {"code": code, "message": message}}

        if not isinstance(params, dict):
            return error(-32602, "Invalid parameters")
        if method == "initialize":
            if self.initialized:
                return error(-32600, "Already initialized")
            version = params.get("protocolVersion")
            if not isinstance(version, str):
                return error(-32602, "Protocol version required")
            self.initialized = True
            return {
                **response,
                "result": {
                    "protocolVersion": version if version in _VERSIONS else "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "synthetic-faq", "version": "1.0.0"},
                },
            }
        if method == "ping":
            return {**response, "result": {}}
        if not self.ready:
            return error(-32600, "Initialization required")
        if method == "tools/list":
            return {**response, "result": {"tools": [_TOOL]}}
        if method != "tools/call":
            return error(-32601, "Method not found")
        if params.get("name") != _TOOL["name"]:
            return error(-32602, "Unknown tool")
        arguments = params.get("arguments")
        topic = arguments.get("topic") if isinstance(arguments, dict) else None
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"topic"}
            or not isinstance(topic, str)
            or topic not in _FAQ
        ):
            result = {
                "content": [{"type": "text", "text": "Invalid fictional FAQ topic"}],
                "isError": True,
            }
        else:
            result = {"content": [{"type": "text", "text": _FAQ[topic]}], "isError": False}
        return {**response, "result": result}


def main() -> int:
    server = FAQServer()
    while True:
        line = sys.stdin.buffer.readline(_MAX_LINE + 1)
        if not line:
            return 0
        if len(line) > _MAX_LINE:
            return 2
        try:
            request = json.loads(line)
        except (ValueError, UnicodeError):
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Invalid JSON"},
            }
        else:
            response = server.dispatch(request)
        if response is not None:
            try:
                print(json.dumps(response), flush=True)
            except BrokenPipeError:
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
