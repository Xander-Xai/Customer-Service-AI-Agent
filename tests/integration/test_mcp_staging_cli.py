"""Full staging CLI wire smoke using the existing local synthetic protocol peer.

No third-party connection, ERP, real customer data or model access.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
FAKE_SERVER = Path(__file__).with_name("fake_mcp_server.py")
EXPECTED = '{"in_stock": true, "name": "测试商品 SKU-1", "price": 99.0, "sku": "SKU-1"}'


@pytest.mark.parametrize(("expected", "exit_code"), [(EXPECTED, 0), ("wrong result", 1)])
def test_staging_cli_real_stdio(tmp_path, expected, exit_code):
    manifest = tmp_path / "manifest.json"
    report = tmp_path / "report.json"
    manifest.write_text(
        json.dumps(
            {
                "server": {
                    "name": "staging",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(FAKE_SERVER)],
                    "risk_level": "low",
                    "allowed_tools": ["get_product_info"],
                    "timeout_seconds": 5,
                },
                "tool": "get_product_info",
                "arguments": {"sku": "SKU-1"},
                "expected_result": expected,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # GNU timeout bounds the complete SDK lifecycle, including same-task close.
    command = [
        "timeout",
        "-k",
        "5s",
        "45s",
        sys.executable,
        "-m",
        "scripts.verify_mcp_staging",
        "--manifest",
        str(manifest),
        "--out",
        str(report),
        "--permit-staging-mcp",
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == exit_code, result.stderr[-1000:]
    raw = report.read_text(encoding="utf-8")
    evidence = json.loads(raw)
    assert evidence["discovery_and_registry"] is True
    assert evidence["unlisted_tool_blocked"] is True
    assert evidence["execution_receipt_ok"] is True
    assert evidence["independent_expected_result_matched"] is (exit_code == 0)
    assert evidence["overall_status"] == ("PASS" if exit_code == 0 else "FAIL")
    assert evidence["production_auth_verified"] is False
    assert evidence["write_operations_verified"] is False
    for private in ("SKU-1", "测试商品", str(FAKE_SERVER), "wrong result"):
        assert private not in raw

    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert second.returncode == 2
    assert report.read_text(encoding="utf-8") == raw
