"""Unit tests for scripts/pytest_counts.py — the local pytest plugin that
writes a machine-readable sidecar of test counts (collected / deselected).

pytest's JUnit XML records only executed/selected counts; it does NOT record
the full ``collected`` count or ``deselected`` count. This plugin captures both
via the ``pytest_itemcollected`` (fires for every item, pre-filter) and
``pytest_deselected`` hooks and writes them to a JSON sidecar so
``scripts/ci_summary.py`` can satisfy the original AC4 requirement
("report distinguishes passed, collected, skipped, failed, and coverage").

These tests run the plugin against a real, throwaway pytest invocation so the
hook contract is proven end-to-end, not just unit-mocked.
"""

from __future__ import annotations

import importlib
import json
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT))
pytest_counts = importlib.import_module("scripts.pytest_counts")


def _run_pytest_with_plugin(testdir: Path, marks: str, extra_args: list[str]) -> dict:
    """Write a tiny test file into ``testdir`` and run pytest with the plugin
    loaded, returning the parsed sidecar dict."""
    testfile = testdir / "test_sample.py"
    testfile.write_text(
        textwrap.dedent(
            f"""
            import pytest

            def test_a():
                pass

            {marks}

            def test_c():
                pass
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    sidecar = testdir / "pytest-counts.json"
    import subprocess

    env = {
        **__import__("os").environ,
        "PYTEST_COUNTS_FILE": str(sidecar),
        "PYTHONPATH": str(REPO_ROOT / "scripts")
        + ":"
        + __import__("os").environ.get("PYTHONPATH", ""),
    }
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(testfile),
            "-p",
            "pytest_counts",
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-cov",
            *extra_args,
        ],
        cwd=str(testdir),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"pytest failed:\n{r.stdout}\n{r.stderr}"
    return json.loads(sidecar.read_text(encoding="utf-8"))


class TestCountsPlugin:
    def test_records_collected_when_nothing_deselected(self, tmp_path):
        # Arrange: 2 tests, no marker filter → both collected, 0 deselected
        # Act
        counts = _run_pytest_with_plugin(tmp_path, marks="# no marker", extra_args=[])

        # Assert
        assert counts["collected"] == 2
        assert counts["deselected"] == 0

    def test_records_collected_and_deselected_with_marker_filter(self, tmp_path):
        # Arrange: 2 tests; test_c marked slow; -m "not slow" deselects it
        # Act
        counts = _run_pytest_with_plugin(
            tmp_path, marks="@pytest.mark.slow", extra_args=["-m", "not slow"]
        )

        # Assert: BOTH collected (2), one deselected (1) — proving the
        # plugin captures the full collected count pre-filter, which JUnit
        # cannot.
        assert counts["collected"] == 2
        assert counts["deselected"] == 1

    def test_sidecar_is_valid_json_with_expected_keys(self, tmp_path):
        # Act
        counts = _run_pytest_with_plugin(tmp_path, marks="# no marker", extra_args=[])

        # Assert
        assert set(counts.keys()) == {"collected", "deselected"}
        assert isinstance(counts["collected"], int)
        assert isinstance(counts["deselected"], int)
