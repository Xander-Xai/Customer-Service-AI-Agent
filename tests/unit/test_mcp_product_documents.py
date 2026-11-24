"""Product document catalog tests; fixtures are not real product evidence."""

import hashlib
import json

import pytest

from tools.readonly_mcp_product_documents import TOOL_NAME, ProductCatalog, ProductDocumentServer


def _record(**changes):
    return {
        "sku": "SKU-001",
        "category": "storage",
        "source_id": "ci-doc-1",
        "revision": "rev-1",
        "valid_from": "2020-01-01",
        "valid_until": "2099-01-01",
        "audience": "customer_public",
        "text": "CI fixture storage instructions.",
        **changes,
    }


def _catalog(tmp_path, records):
    raw = json.dumps({"schema": "customer-product-documents/v1", "records": records}).encode()
    path = tmp_path / "private-docs.json"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_private_catalog_loads_pinned_source_and_expiry(tmp_path):
    path, digest = _catalog(
        tmp_path, [_record(), _record(sku="EXPIRED", valid_until="2021-01-01")]
    )
    catalog = ProductCatalog(path, digest)
    assert catalog.lookup("SKU-001", "storage")["catalog_sha256"] == digest
    assert catalog.lookup("EXPIRED", "storage")["status"] == "NOT_AVAILABLE"
    assert catalog.lookup("UNKNOWN", "storage")["status"] == "NOT_AVAILABLE"
    with pytest.raises(ValueError):
        ProductCatalog(path, "a" * 64)


@pytest.mark.parametrize("records", [[_record(audience="staff_only")], [_record(), _record()]])
def test_catalog_rejects_non_public_or_ambiguous_records(tmp_path, records):
    path, digest = _catalog(tmp_path, records)
    with pytest.raises(ValueError):
        ProductCatalog(path, digest)


def test_server_never_exposes_demo_or_write_tools(tmp_path):
    path, digest = _catalog(tmp_path, [_record()])
    server = ProductDocumentServer(ProductCatalog(path, digest))

    def rpc(method, params=None):
        return server.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        )

    assert "error" in rpc("tools/list")
    rpc("initialize", {"protocolVersion": "2025-06-18"})
    server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert [tool["name"] for tool in rpc("tools/list")["result"]["tools"]] == [TOOL_NAME]
    assert "error" in rpc("tools/call", {"name": "refund", "arguments": {}})
    result = rpc(
        "tools/call", {"name": TOOL_NAME, "arguments": {"sku": "SKU-001", "category": "storage"}}
    )
    assert result["result"]["structuredContent"]["source_id"] == "ci-doc-1"
    bad = rpc(
        "tools/call", {"name": TOOL_NAME, "arguments": {"sku": "../private", "category": "storage"}}
    )
    assert bad["result"]["isError"] is True
