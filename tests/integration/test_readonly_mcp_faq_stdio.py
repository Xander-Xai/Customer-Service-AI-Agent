"""Official client -> ToolRegistry -> standalone fictional FAQ process."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "linux", reason="GNU timeout required"),
]


def test_standalone_faq_mcp_stdio(tmp_path):
    pytest.importorskip("mcp")
    root = Path(__file__).resolve().parents[2]
    manifest = tmp_path / "manifest.json"
    report = tmp_path / "report.json"
    manifest.write_text(
        json.dumps(
            {
                "server": {
                    "name": "syntheticfaq",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(root / "tools" / "readonly_mcp_faq.py")],
                    "risk_level": "low",
                    "allowed_tools": ["lookup_demo_faq"],
                    "timeout_seconds": 5,
                },
                "tool": "lookup_demo_faq",
                "arguments": {"topic": "demo_shipping"},
                "expected_result": "Fictional example: shipping takes 3 demo days.",
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
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
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert evidence["overall_status"] == "PASS"
    assert evidence["unlisted_tool_blocked"] is True
    assert evidence["production_auth_verified"] is False
