"""Official MCP client and actual subprocess with operator-provided fixture catalog."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts.verify_mcp_staging import run_probe

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_business_product_document_through_registry(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    raw = json.dumps(
        {
            "schema": "customer-product-documents/v1",
            "records": [
                {
                    "sku": "CI-SKU",
                    "category": "usage",
                    "source_id": "ci-document",
                    "revision": "r1",
                    "valid_from": "2020-01-01",
                    "valid_until": "2099-01-01",
                    "audience": "customer_public",
                    "text": "CI fixture document.",
                }
            ],
        }
    ).encode()
    catalog = tmp_path / "catalog.json"
    catalog.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    tool = "lookup_product_support_document"
    executable = Path(__file__).resolve().parents[2] / "tools" / "readonly_mcp_product_documents.py"
    monkeypatch.chdir(tmp_path)
    report = await run_probe(
        {
            "server": {
                "name": "productdocs",
                "transport": "stdio",
                "command": sys.executable,
                "args": [
                    str(executable),
                    "--catalog",
                    str(catalog),
                    "--sha256",
                    digest,
                    "--authorized-customer-disclosure",
                ],
                "risk_level": "low",
                "allowed_tools": [tool],
                "timeout_seconds": 5,
            },
            "tool": tool,
            "arguments": {"sku": "CI-SKU", "category": "usage"},
            "expected_result": {
                "status": "FOUND",
                "sku": "CI-SKU",
                "category": "usage",
                "source_id": "ci-document",
                "revision": "r1",
                "valid_until": "2099-01-01",
                "catalog_sha256": digest,
                "text": "CI fixture document.",
                "provenance": "OPERATOR_APPROVED_CUSTOMER_PUBLIC_CATALOG",
            },
        }
    )
    assert report["overall_status"] == "PASS", report
    assert report["production_auth_verified"] is False
