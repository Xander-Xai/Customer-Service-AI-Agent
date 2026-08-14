"""Unit tests for scripts/ci_summary.py (P0-01 AC4 machine-readable summary).

These prove the summary helper correctly extracts passed/failed/skipped/errors/
executed from JUnit XML, collected/deselected from the pytest-counts.json
sidecar (which JUnit does not record), and coverage from coverage.xml — the
AC4 "report distinguishes passed, collected, skipped, failed and coverage"
requirement — and degrades gracefully so the informational summary never fails
the job.
"""

from __future__ import annotations

import importlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Import the helper as a module under scripts.ci_summary.
sys.path.insert(0, str(REPO_ROOT))
ci_summary = importlib.import_module("scripts.ci_summary")


def _write_junit(path: Path, *, tests: int, failures: int, errors: int, skipped: int) -> None:
    """Write a minimal JUnit XML report (pytest format)."""
    root = ET.Element(
        "testsuite",
        {
            "name": "pytest",
            "tests": str(tests),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
        },
    )
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _write_coverage(path: Path, *, line_rate: float) -> None:
    """Write a minimal coverage.xml like coverage.py produces."""
    root = ET.Element("coverage", {"line-rate": f"{line_rate:.4f}", "version": "7.1.0"})
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _write_counts(path: Path, *, collected: int, deselected: int) -> None:
    """Write the pytest-counts.json sidecar produced by pytest_counts.py."""
    import json

    path.write_text(
        json.dumps({"collected": collected, "deselected": deselected}), encoding="utf-8"
    )


class TestBuildSummary:
    def test_surfaces_all_counts_and_subthreshold_coverage_gate_fail(self, tmp_path):
        # Arrange: 10 executed, 2 failed, 1 skipped → 7 passed; coverage 78.72%
        junit = tmp_path / "pytest-report.xml"
        cov = tmp_path / "coverage.xml"
        _write_junit(junit, tests=10, failures=2, errors=0, skipped=1)
        _write_coverage(cov, line_rate=0.7872)

        # Act — no sidecar (legacy two-arg call)
        summary = ci_summary.build_summary(junit, cov)

        # Assert: JUnit-derived counts present, gate fails.
        assert "Executed: 10" in summary
        assert "Passed: 7" in summary
        assert "Failed: 2" in summary
        assert "Skipped: 1" in summary
        assert "Coverage: 78.72%" in summary
        assert "FAIL (blocking, fail_under=80%)" in summary
        # Without the sidecar, Collected/Deselected are marked unavailable —
        # the helper never fabricates them from JUnit's tests attr.
        assert "Collected/Deselected: unavailable" in summary

    def test_passing_coverage_gate_marks_pass(self, tmp_path):
        # Arrange: coverage above the gate
        junit = tmp_path / "pytest-report.xml"
        cov = tmp_path / "coverage.xml"
        _write_junit(junit, tests=5, failures=0, errors=0, skipped=0)
        _write_coverage(cov, line_rate=0.85)

        # Act
        summary = ci_summary.build_summary(junit, cov)

        # Assert
        assert "Coverage: 85.00%" in summary
        assert "gate: PASS" in summary
        assert "Passed: 5" in summary

    def test_errors_reduce_passed_count(self, tmp_path):
        # Arrange: errors also reduce the passed count (passed = tests - failures - errors - skipped)
        junit = tmp_path / "pytest-report.xml"
        cov = tmp_path / "coverage.xml"
        _write_junit(junit, tests=8, failures=1, errors=2, skipped=1)
        _write_coverage(cov, line_rate=0.90)

        # Act
        summary = ci_summary.build_summary(junit, cov)

        # Assert: 8 - 1 - 2 - 1 = 4 passed
        assert "Passed: 4" in summary
        assert "Errors: 2" in summary


class TestCountsSidecarIntegration:
    """When the pytest-counts.json sidecar is provided, the summary surfaces
    Collected and Deselected accurately — satisfying the original AC4 spec
    wording ("report distinguishes passed, collected, skipped, failed")."""

    def test_surfaces_collected_and_deselected_from_sidecar(self, tmp_path):
        # Arrange: 12 collected, 2 deselected → 10 executed; all 10 pass;
        # coverage below gate.
        junit = tmp_path / "pytest-report.xml"
        cov = tmp_path / "coverage.xml"
        counts = tmp_path / "pytest-counts.json"
        _write_junit(junit, tests=10, failures=0, errors=0, skipped=0)
        _write_coverage(cov, line_rate=0.7872)
        _write_counts(counts, collected=12, deselected=2)

        # Act
        summary = ci_summary.build_summary(junit, cov, counts)

        # Assert: ALL AC4 fields present and accurate.
        assert "Collected: 12" in summary
        assert "Deselected: 2" in summary
        assert "Executed: 10" in summary
        assert "Passed: 10" in summary
        assert "Failed: 0" in summary
        assert "Skipped: 0" in summary
        assert "Coverage: 78.72%" in summary
        assert "FAIL (blocking, fail_under=80%)" in summary

    def test_sidecar_missing_degrades_gracefully(self, tmp_path):
        # Arrange: JUnit + coverage present, sidecar path points to missing file
        junit = tmp_path / "pytest-report.xml"
        cov = tmp_path / "coverage.xml"
        _write_junit(junit, tests=3, failures=0, errors=0, skipped=0)
        _write_coverage(cov, line_rate=0.80)
        missing = tmp_path / "pytest-counts.json"  # never written

        # Act
        summary = ci_summary.build_summary(junit, cov, missing)

        # Assert: executed counts still shown; Collected/Deselected unavailable
        assert "Executed: 3" in summary
        assert "Collected/Deselected: unavailable" in summary

    def test_malformed_sidecar_degrades_gracefully(self, tmp_path):
        # Arrange: sidecar is not valid JSON
        junit = tmp_path / "pytest-report.xml"
        cov = tmp_path / "coverage.xml"
        counts = tmp_path / "pytest-counts.json"
        _write_junit(junit, tests=3, failures=0, errors=0, skipped=0)
        _write_coverage(cov, line_rate=0.80)
        counts.write_text("{not valid json", encoding="utf-8")

        # Act — must not raise
        summary = ci_summary.build_summary(junit, cov, counts)

        # Assert
        assert "Collected/Deselected: unavailable" in summary
        assert "Executed: 3" in summary

    def test_provenance_line_present(self, tmp_path):
        # Arrange
        junit = tmp_path / "pytest-report.xml"
        cov = tmp_path / "coverage.xml"
        _write_junit(junit, tests=1, failures=0, errors=0, skipped=0)
        _write_coverage(cov, line_rate=0.80)

        # Act
        summary = ci_summary.build_summary(junit, cov)

        # Assert: provenance line present (Python version + git SHA) so the
        # coverage figure is reproducible/attributable.
        assert "Provenance: Python " in summary
        assert "git " in summary


class TestProvenance:
    """AC8 reproducibility: a coverage figure is only meaningful if the
    environment that produced it is recorded. The provenance line must carry
    Python version, git SHA, the resolved pytest/pytest-cov/coverage/
    pytest-asyncio versions (the toolchain that produced the number — the
    78.36 vs 78.72 gap was caused by pytest version skew, invisible without
    these), a requirements.txt content hash, the COVERAGE_FILE isolation
    status, and the recorded pytest invocation command.
    """

    def _artifacts(self, tmp_path):
        junit = tmp_path / "pytest-report.xml"
        cov = tmp_path / "coverage.xml"
        _write_junit(junit, tests=1, failures=0, errors=0, skipped=0)
        _write_coverage(cov, line_rate=0.80)
        return junit, cov

    def test_provenance_includes_toolchain_package_versions(self, tmp_path):
        # Arrange
        junit, cov = self._artifacts(tmp_path)

        # Act
        summary = ci_summary.build_summary(junit, cov)

        # Assert: the resolved versions of the four toolchain packages appear
        # — these are what actually produced the coverage number.
        assert "pytest" in summary.lower()
        assert "pytest-cov" in summary.lower()
        assert "coverage" in summary.lower()
        assert "pytest-asyncio" in summary.lower()
        # A version-looking token (digits) accompanies the pytest mention.
        import re

        assert re.search(r"pytest[^\n]*\d+\.\d+", summary), (
            "provenance should include a pytest version with digits, got:\n" + summary
        )

    def test_provenance_includes_requirements_hash(self, tmp_path):
        # Arrange
        junit, cov = self._artifacts(tmp_path)

        # Act
        summary = ci_summary.build_summary(junit, cov)

        # Assert: a requirements.txt content-hash token is present (drift
        # detection for the dependency declaration CI installs).
        assert "requirements.txt" in summary.lower()
        assert "sha256:" in summary.lower(), (
            "provenance should include a sha256 hash of requirements.txt, got:\n" + summary
        )

    def test_provenance_includes_coverage_file_isolation_status(self, tmp_path, monkeypatch):
        # Arrange: an isolated COVERAGE_FILE (the reviewer's "is COVERAGE_FILE
        # isolated?" question answered in the provenance itself).
        junit, cov = self._artifacts(tmp_path)
        monkeypatch.setenv("COVERAGE_FILE", "/tmp/isolated-run.coverage")

        # Act
        summary = ci_summary.build_summary(junit, cov)

        # Assert: the isolation path is surfaced.
        assert "COVERAGE_FILE=/tmp/isolated-run.coverage" in summary

    def test_provenance_reports_default_coverage_file_when_unset(self, tmp_path, monkeypatch):
        # Arrange
        junit, cov = self._artifacts(tmp_path)
        monkeypatch.delenv("COVERAGE_FILE", raising=False)

        # Act
        summary = ci_summary.build_summary(junit, cov)

        # Assert: absence is explicit, not silent.
        assert "COVERAGE_FILE=default" in summary

    def test_provenance_includes_recorded_invocation(self, tmp_path, monkeypatch):
        # Arrange: the workflow records the exact pytest command in
        # PYTEST_INVOCATION so the coverage figure is attributable to a
        # specific command (the reviewer's "完整命令" requirement).
        junit, cov = self._artifacts(tmp_path)
        monkeypatch.setenv("PYTEST_INVOCATION", "python -m pytest tests/unit/ --cov=.")

        # Act
        summary = ci_summary.build_summary(junit, cov)

        # Assert
        assert "python -m pytest tests/unit/ --cov=." in summary

    def test_provenance_reports_unrecorded_invocation_when_unset(self, tmp_path, monkeypatch):
        # Arrange
        junit, cov = self._artifacts(tmp_path)
        monkeypatch.delenv("PYTEST_INVOCATION", raising=False)

        # Act
        summary = ci_summary.build_summary(junit, cov)

        # Assert: absence is explicit.
        assert "unrecorded" in summary.lower()

    def test_provenance_degrades_gracefully_when_package_missing(self, tmp_path, monkeypatch):
        # Arrange: simulate a toolchain package not being importable (e.g.
        # pytest-asyncio absent in a minimal env). Provenance must surface
        # "unknown" rather than crash the informational summary.
        junit, cov = self._artifacts(tmp_path)

        original = ci_summary.version

        def fake_version(name):
            if name == "pytest-asyncio":
                raise ci_summary.PackageNotFoundError(name)
            return original(name)

        # Patch the name *as bound in ci_summary* (it did
        # `from importlib.metadata import version` at import time, so
        # patching importlib.metadata.version would not reach it).
        monkeypatch.setattr(ci_summary, "version", fake_version)

        # Act — must not raise
        summary = ci_summary.build_summary(junit, cov)

        # Assert: the missing package shows "unknown", the rest still render.
        assert "unknown" in summary.lower()
        assert "Provenance:" in summary


class TestGracefulDegradation:
    def test_missing_junit_report_degrades_gracefully(self, tmp_path):
        # Arrange: coverage present, junit absent
        cov = tmp_path / "coverage.xml"
        _write_coverage(cov, line_rate=0.80)
        missing_junit = tmp_path / "pytest-report.xml"  # never written

        # Act
        summary = ci_summary.build_summary(missing_junit, cov)

        # Assert: test counts marked unavailable, coverage still shown
        assert "Test counts: unavailable" in summary
        assert "Coverage: 80.00%" in summary

    def test_missing_coverage_report_degrades_gracefully(self, tmp_path):
        # Arrange: junit present, coverage absent
        junit = tmp_path / "pytest-report.xml"
        _write_junit(junit, tests=3, failures=0, errors=0, skipped=0)
        missing_cov = tmp_path / "coverage.xml"  # never written

        # Act
        summary = ci_summary.build_summary(junit, missing_cov)

        # Assert: coverage marked unavailable, test counts still shown
        assert "Coverage: unavailable" in summary
        assert "Passed: 3" in summary

    def test_main_exits_zero_when_both_reports_missing(self, tmp_path, monkeypatch):
        # Arrange: run from a dir with neither report; must exit 0 (informational)
        monkeypatch.chdir(tmp_path)

        # Act
        rc = ci_summary.main([])

        # Assert: informational summary never fails the job
        assert rc == 0

    def test_malformed_numeric_junit_attr_degrades_gracefully(self, tmp_path):
        # Arrange: JUnit with a non-numeric `tests` attr (P0-01 acceptance
        # review found int() was not guarded → ValueError). Must degrade,
        # not raise.
        junit = tmp_path / "pytest-report.xml"
        cov = tmp_path / "coverage.xml"
        root = ET.Element("testsuite", {"tests": "not-an-int", "failures": "0"})
        ET.ElementTree(root).write(junit, encoding="utf-8", xml_declaration=True)
        _write_coverage(cov, line_rate=0.80)

        # Act — must not raise
        summary = ci_summary.build_summary(junit, cov)

        # Assert: test counts marked unavailable (malformed), coverage shown
        assert "Test counts: unavailable" in summary
        assert "Coverage: 80.00%" in summary
