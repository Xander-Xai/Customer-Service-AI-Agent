"""Official MCP client and actual subprocess with operator-provided fixture catalog."""

import hashlib
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.prepare_standard_protocol_config import (
    PRODUCT_CATALOG,
)
from scripts.prepare_standard_protocol_config import (
    main as prepare_protocol_config,
)
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


@pytest.mark.asyncio
async def test_standard_compose_product_document_config_roundtrip(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setenv("CSAI_PROTOCOL_MODE", "synthetic")
    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "standard")
    monkeypatch.setattr("scripts.prepare_standard_protocol_config.os.chown", lambda *_: None)
    monkeypatch.setattr(
        "scripts.prepare_standard_protocol_config.pwd.getpwnam",
        lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000),
    )
    monkeypatch.setattr(
        "sys.argv", ["prepare-standard-protocols", "--output-directory", str(tmp_path)]
    )
    assert prepare_protocol_config() == 0

    environment = (tmp_path / "protocol.env").read_text(encoding="ascii")
    encoded = next(
        line.split("=", 1)[1]
        for line in environment.splitlines()
        if line.startswith("MCP_SERVERS=")
    )
    server = json.loads(shlex.split(encoded)[0])[0]
    catalog_digest = server["args"][4]
    executable = Path(__file__).resolve().parents[2] / "tools" / "readonly_mcp_product_documents.py"
    catalog = PRODUCT_CATALOG.resolve()
    server["command"] = sys.executable
    server["args"][0] = str(executable)
    server["args"][2] = str(catalog)
    monkeypatch.chdir(tmp_path)

    expected = {
        "status": "FOUND",
        "sku": "CSAI-DEMO-001",
        "category": "usage",
        "source_id": "synthetic-product-guide",
        "revision": "1",
        "valid_until": "2099-12-31",
        "catalog_sha256": catalog_digest,
        "text": "Synthetic local demonstration only: follow the product label and discontinue use if irritation occurs. This fixture is not medical advice.",
        "provenance": "SYNTHETIC_LOCAL_FIXTURE",
    }
    report = await run_probe(
        {
            "server": server,
            "tool": "lookup_product_support_document",
            "arguments": {"sku": "CSAI-DEMO-001", "category": "usage"},
            "expected_result": expected,
        }
    )
    assert report["overall_status"] == "PASS", report
    assert report["production_auth_verified"] is False
