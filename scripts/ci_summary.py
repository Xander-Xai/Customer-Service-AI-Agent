#!/usr/bin/env python3
"""CI summary helper (P0-01 Required Change #5 / AC4).

Parses three machine-readable artifacts produced by the CI coverage step and
emits a markdown summary of passed/collected/deselected/executed/failed/
skipped/errors + coverage % to stdout (redirected to ``$GITHUB_STEP_SUMMARY``
in the workflow):

- ``pytest-report.xml`` (JUnit) → executed/passed/failed/errors/skipped.
  pytest's JUnit does NOT record ``collected`` or ``deselected``.
- ``pytest-counts.json`` (sidecar from ``scripts/pytest_counts.py``) →
  ``collected`` + ``deselected``. This is what makes the report satisfy the
  original AC4 spec wording ("report distinguishes passed, collected, skipped,
  failed, and coverage") accurately — without fabricating counts JUnit lacks.
- ``coverage.xml`` → line-coverage % and the ``fail_under`` gate status.

Also emits a provenance line (Python version + git SHA) so coverage figures are
reproducible. Exits 0 and degrades gracefully if any file is missing or
malformed — this is an *informational* summary and must never fail the job
(the blocking gates live in the pytest/coverage steps themselves).

Usage::

    python scripts/ci_summary.py [pytest-report.xml] [coverage.xml] [pytest-counts.json]
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

COVERAGE_GATE = 80.0

REPO_ROOT = Path(__file__).resolve().parent.parent

# The toolchain packages whose resolved versions produced the coverage number.
# Recording them makes a coverage figure attributable to a specific
# environment — the 78.36 vs 78.72 gap in P0-01 acceptance was pytest version
# skew (CI pins pytest==7.4.4; local had 8.4.2), invisible without these.
_PROVENANCE_PACKAGES = ("pytest", "pytest-cov", "coverage", "pytest-asyncio")


def _pkg_version(name: str) -> str:
    """Return the installed distribution version of ``name``, or ``"unknown"``
    if it is not installed (provenance must never raise)."""
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def _requirements_hash() -> str:
    """Return the first 12 hex chars of the sha256 of ``requirements.txt``'s
    content, or ``"unavailable"`` if the file cannot be read. This is drift
    detection for the dependency declaration CI installs (CI runs
    ``pip install -r requirements.txt``)."""
    try:
        digest = hashlib.sha256((REPO_ROOT / "requirements.txt").read_bytes()).hexdigest()
    except OSError:
        return "unavailable"
    return digest[:12]


def _coverage_file_env() -> str:
    """Return the ``COVERAGE_FILE`` env value, or ``"default"`` (the implicit
    ``.coverage`` in the CWD). Surfacing this answers the acceptance reviewer's
    "is COVERAGE_FILE isolated?" directly in the provenance."""
    return os.environ.get("COVERAGE_FILE", "default")


def _pytest_invocation() -> str:
    """Return the pytest command the workflow recorded in ``PYTEST_INVOCATION``,
    or ``"unrecorded"`` when the env var is unset (e.g. local/unit-test runs)."""
    return os.environ.get("PYTEST_INVOCATION", "unrecorded")


def _test_counts(junit_path: Path) -> dict[str, int] | str:
    """Return ``{executed, passed, failed, errors, skipped}`` parsed from the
    JUnit XML, or an ``"unavailable: …"`` string if the file is missing or
    malformed (including non-numeric count attributes).

    ``executed`` is JUnit's ``tests`` attribute — the number of tests pytest
    *selected and ran*. pytest does not record ``collected`` or ``deselected``
    in JUnit XML; those come from the ``pytest-counts.json`` sidecar.
    """
    try:
        root = ET.parse(junit_path).getroot()
    except (FileNotFoundError, ET.ParseError) as e:
        return f"unavailable: {e}"
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    try:
        tests = failures = errors = skipped = 0
        for suite in suites:
            if suite.tag != "testsuite":
                continue
            attrib = suite.attrib
            tests += int(attrib.get("tests", 0))
            failures += int(attrib.get("failures", 0))
            errors += int(attrib.get("errors", 0))
            skipped += int(attrib.get("skipped", 0))
    except (ValueError, TypeError) as e:
        return f"unavailable: malformed count attribute: {e}"
    passed = max(tests - failures - errors - skipped, 0)
    return {
        "executed": tests,
        "passed": passed,
        "failed": failures,
        "errors": errors,
        "skipped": skipped,
    }


def _collected_deselected(counts_path: Path) -> dict[str, int] | str:
    """Return ``{collected, deselected}`` from the ``pytest-counts.json``
    sidecar written by ``scripts/pytest_counts.py``, or an ``"unavailable: …"``
    string if missing/malformed."""
    try:
        data = json.loads(counts_path.read_text(encoding="utf-8"))
        return {
            "collected": int(data["collected"]),
            "deselected": int(data["deselected"]),
        }
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        return f"unavailable: {e}"


def _coverage_rate(cov_path: Path) -> float | str:
    """Return the line-coverage percentage from ``coverage.xml``, or an
    ``"unavailable: …"`` string if the file is missing or malformed."""
    try:
        root = ET.parse(cov_path).getroot()
        return float(root.attrib.get("line-rate", "0")) * 100
    except (FileNotFoundError, ET.ParseError, ValueError, TypeError) as e:
        return f"unavailable: {e}"


def _git_sha() -> str:
    """Return the short git SHA of HEAD, or ``"unknown"`` if git is unavailable
    or not a repo (provenance must never fail the summary)."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def build_summary(junit_path: Path, cov_path: Path, counts_path: Path | None = None) -> str:
    """Build the markdown summary string from the reports.

    ``counts_path`` (the ``pytest-counts.json`` sidecar) is optional so callers
    without it still get the JUnit/coverage fields — but when present it adds
    the ``Collected``/``Deselected`` fields that satisfy AC4 accurately.
    """
    lines = ["### Test & Coverage Summary"]
    counts = _test_counts(junit_path)
    cd = (
        _collected_deselected(counts_path)
        if counts_path is not None
        else "unavailable: no sidecar path provided"
    )
    if isinstance(counts, str):
        lines.append(f"- Test counts: {counts}")
    elif isinstance(cd, str):
        # JUnit present but sidecar missing — still report executed counts.
        lines.append(
            f"- Executed: {counts['executed']} | Passed: {counts['passed']} | "
            f"Failed: {counts['failed']} | Errors: {counts['errors']} | "
            f"Skipped: {counts['skipped']}"
        )
        lines.append(f"- Collected/Deselected: {cd}")
    else:
        lines.append(
            f"- Collected: {cd['collected']} | Deselected: {cd['deselected']} | "
            f"Executed: {counts['executed']} | Passed: {counts['passed']} | "
            f"Failed: {counts['failed']} | Errors: {counts['errors']} | "
            f"Skipped: {counts['skipped']}"
        )
    rate = _coverage_rate(cov_path)
    if isinstance(rate, str):
        lines.append(f"- Coverage: {rate}")
    else:
        gate = (
            "PASS" if rate >= COVERAGE_GATE else f"FAIL (blocking, fail_under={COVERAGE_GATE:.0f}%)"
        )
        lines.append(f"- Coverage: {rate:.2f}% — gate: {gate}")
    # Provenance (AC8 reproducibility): a coverage figure is only meaningful if
    # the environment that produced it is recorded. Python ver + git SHA + the
    # resolved toolchain versions (the 78.36↔78.72 gap was pytest version skew)
    # + a requirements.txt hash (dep-declaration drift) + COVERAGE_FILE
    # isolation status + the recorded pytest invocation.
    pkgs = " / ".join(f"{name} {_pkg_version(name)}" for name in _PROVENANCE_PACKAGES)
    lines.append(
        f"- Provenance: Python {platform.python_version()} @ git {_git_sha()} "
        f"| {pkgs} | requirements.txt sha256:{_requirements_hash()} "
        f"| COVERAGE_FILE={_coverage_file_env()} | invocation: {_pytest_invocation()}"
    )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    junit = Path(argv[1]) if len(argv) > 1 else Path("pytest-report.xml")
    cov = Path(argv[2]) if len(argv) > 2 else Path("coverage.xml")
    counts = Path(argv[3]) if len(argv) > 3 else Path("pytest-counts.json")
    print(build_summary(junit, cov, counts))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
