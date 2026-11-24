"""Operator-configured MCP product documentation, not a fictional FAQ.

The catalog is a private deployment input approved for customer disclosure.
Only an exact product SKU and document category can be queried. No database
credentials, customer IDs, filesystem paths, writes, or network calls are
accepted from a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from tools.readonly_mcp_faq import FAQServer

MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 5000
SKU = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
CATEGORIES = ("usage", "storage", "packaging")
TOOL_NAME = "lookup_product_support_document"
TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Read approved customer-facing product usage, storage or packaging instructions by exact SKU. "
        "Never use this for medical/regulatory advice, inventory, order or refund decisions."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "sku": {"type": "string"},
            "category": {"type": "string", "description": "One of usage, storage, packaging."},
        },
        "required": ["sku", "category"],
    },
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate catalog field")
        result[key] = value
    return result


def _date(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Invalid effective date")
    return date.fromisoformat(value)


class ProductCatalog:
    def __init__(self, path: Path, expected_sha256: str):
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("Pinned catalog digest required")
        with path.open("rb") as handle:
            raw = handle.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("Catalog digest or size mismatch")
        content = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(content, dict) or set(content) != {"schema", "records"}:
            raise ValueError("Invalid catalog envelope")
        if content["schema"] != "customer-product-documents/v1":
            raise ValueError("Invalid catalog schema")
        records = content["records"]
        if not isinstance(records, list) or not 1 <= len(records) <= MAX_RECORDS:
            raise ValueError("Invalid catalog population")
        self.digest = expected_sha256
        self.records = {}
        for record in records:
            if not isinstance(record, dict) or set(record) != {
                "sku", "category", "source_id", "revision", "valid_from", "valid_until", "audience", "text"
            }:
                raise ValueError("Invalid document fields")
            if (
                not isinstance(record["sku"], str)
                or not SKU.fullmatch(record["sku"])
                or record["category"] not in CATEGORIES
                or record["audience"] != "customer_public"
                or any(
                    not isinstance(record[key], str) or not IDENTIFIER.fullmatch(record[key])
                    for key in ("source_id", "revision")
                )
                or not isinstance(record["text"], str)
                or not 1 <= len(record["text"].strip()) <= 1200
                or _date(record["valid_from"]) > _date(record["valid_until"])
            ):
                raise ValueError("Invalid or non-customer-facing document")
            key = (record["sku"], record["category"])
            if key in self.records:
                raise ValueError("Ambiguous document revision")
            self.records[key] = record

    def lookup(self, sku: str, category: str) -> dict:
        if not isinstance(sku, str) or not SKU.fullmatch(sku) or category not in CATEGORIES:
            raise ValueError("Invalid product/document selection")
        today = datetime.now(timezone.utc).date()
        record = self.records.get((sku, category))
        if not record or not _date(record["valid_from"]) <= today <= _date(record["valid_until"]):
            return {"status": "NOT_AVAILABLE", "sku": sku, "category": category}
        return {
            "status": "FOUND",
            "sku": sku,
            "category": category,
            "source_id": record["source_id"],
            "revision": record["revision"],
            "valid_until": record["valid_until"],
            "catalog_sha256": self.digest,
            "text": record["text"],
            "provenance": "OPERATOR_APPROVED_CUSTOMER_PUBLIC_CATALOG",
        }


class ProductDocumentServer(FAQServer):
    """Use the existing bounded MCP handshake; replace the business surface."""

    def __init__(self, catalog: ProductCatalog):
        super().__init__()
        self.catalog = catalog

    def dispatch(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            return super().dispatch(request)
        method = request.get("method")
        if method not in {"tools/list", "tools/call"} or not self.ready:
            response = super().dispatch(request)
            if method == "initialize" and response and "result" in response:
                response["result"]["serverInfo"] = {"name": "product-documents", "version": "1.0.0"}
            return response
        if "id" not in request:
            return None
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("params", {}), dict):
            return {**response, "error": {"code": -32600, "message": "Invalid request"}}
        if method == "tools/list":
            return {**response, "result": {"tools": [TOOL]}}
        params = request.get("params", {})
        if params.get("name") != TOOL_NAME:
            return {**response, "error": {"code": -32602, "message": "Unknown tool"}}
        arguments = params.get("arguments")
        try:
            if not isinstance(arguments, dict) or set(arguments) != {"sku", "category"}:
                raise ValueError("Invalid arguments")
            result = self.catalog.lookup(arguments["sku"], arguments["category"])
        except (TypeError, ValueError):
            return {
                **response,
                "result": {"content": [{"type": "text", "text": "Invalid document request"}], "isError": True},
            }
        return {
            **response,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": result,
                "isError": False,
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--authorized-customer-disclosure", action="store_true", required=True)
    args = parser.parse_args()
    try:
        server = ProductDocumentServer(ProductCatalog(args.catalog, args.sha256))
    except (OSError, TypeError, ValueError):
        print("Product catalog unavailable or invalid", file=sys.stderr)
        return 2
    while True:
        line = sys.stdin.buffer.readline(32769)
        if not line:
            return 0
        if len(line) > 32768:
            return 2
        try:
            request = json.loads(line)
        except (ValueError, UnicodeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}
        else:
            response = server.dispatch(request)
        if response is not None:
            try:
                print(json.dumps(response), flush=True)
            except BrokenPipeError:
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
