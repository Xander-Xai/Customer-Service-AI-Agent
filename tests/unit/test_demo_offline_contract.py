"""Contract tests for the one-command offline demo (Issue #120).

These prove the demo's *honesty properties*: the verdict is derived from the
real process exit status, a failure/no-op is never reported as PASS, the egress
guard blocks real network access, and the Makefile exposes the single command.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "scripts" / "demo_offline.py"


def _load_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    import demo_offline

    return demo_offline


class TestVerdictMapping:
    def test_pass_only_on_zero(self):
        mod = _load_module()
        assert mod.verdict_for_return_code(0) == "PASS"

    def test_no_tests_collected_is_not_run(self):
        mod = _load_module()
        assert mod.verdict_for_return_code(5) == "NOT_RUN"

    @pytest.mark.parametrize("rc", [1, 2, 3, 4, 99])
    def test_any_other_code_is_fail(self, rc):
        mod = _load_module()
        assert mod.verdict_for_return_code(rc) == "FAIL"


class TestProofCard:
    def test_all_required_fields_present(self):
        mod = _load_module()
        card = mod.build_proof_card(
            verdict="PASS",
            return_code=0,
            command=mod.build_command(mod.DEFAULT_NODES),
            git_sha="deadbeef",
            timestamp="2026-10-07T00:00:00Z",
        )
        for field in (
            "scenario",
            "mode",
            "source_anchor",
            "test_anchor",
            "git_sha",
            "timestamp_utc",
            "test_command",
            "verdict",
            "environment_limitations",
        ):
            assert field in card, f"proof card missing field: {field}"
        assert card["mode"] == "MOCK/OFFLINE"
        assert card["test_anchor"], "test anchors must not be empty"


class TestEgressGuard:
    def test_guard_blocks_dns_and_connect(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import socket

        from offline_egress_guard import install_egress_guard

        # The guard patches process-global socket/httpx functions; restore them so
        # this test does not leak the block into the rest of the pytest session.
        restore = install_egress_guard()
        try:
            with pytest.raises(AssertionError):
                socket.getaddrinfo("example.com", 80)
            with pytest.raises(AssertionError):
                socket.socket().connect(("93.184.216.34", 80))
        finally:
            restore()


class TestSingleCommandWiring:
    def test_makefile_defines_demo_offline(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        assert re.search(
            r"^demo-offline:", makefile, re.MULTILINE
        ), "Makefile must define the demo-offline target"
        assert "demo-offline" in makefile.splitlines()[0], "demo-offline must be declared .PHONY"

    def test_failing_selection_exits_nonzero_and_reports_fail(self, tmp_path):
        """A no-op/failed selection must never be presented as PASS."""
        json_out = tmp_path / "card.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(DEMO),
                "--node",
                "tests/unit/this_node_does_not_exist.py::nope",
                "--json-out",
                str(json_out),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0, "failed demo must return non-zero"
        assert "FAIL" in completed.stdout
        card = json.loads(json_out.read_text(encoding="utf-8"))
        assert card["verdict"] == "FAIL"
        assert card["pytest_return_code"] != 0
