"""Local pytest plugin: write a machine-readable test-counts sidecar.

pytest's JUnit XML records only the *executed/selected* count (the ``tests``
attribute) and ``failures``/``errors``/``skipped``; it does **not** record the
full ``collected`` count or the ``deselected`` count. The P0-01 spec (AC4)
requires the report to distinguish ``passed, collected, skipped, failed`` — so
``collected`` (and ``deselected``) must come from elsewhere.

This plugin captures them via:

- ``pytest_itemcollected`` — fires for every collected item *before* the
  ``-m`` marker filter runs, so it counts the true collected set.
- ``pytest_deselected`` — fires for items dropped by ``-m`` / ``--deselect``.

At session end it writes ``{"collected": int, "deselected": int}`` to the path
in ``$PYTEST_COUNTS_FILE`` (default ``pytest-counts.json``). ``ci_summary.py``
reads this sidecar alongside the JUnit and coverage reports.

Loaded in CI via ``PYTHONPATH=scripts pytest -p pytest_counts …``. No third-party
dependency; stdlib only.
"""

from __future__ import annotations

import json
import os

_collected = 0
_deselected = 0


def pytest_itemcollected(item):  # noqa: ARG001 - pytest hook signature
    """Count every collected item. Fires before the marker filter, so this is
    the true ``collected`` count (not the post-filter executed count)."""
    global _collected
    _collected += 1


def pytest_deselected(items):
    """Count items dropped by ``-m`` / ``--deselect`` / ``--ignore``-style
    filtering (the ones pytest reports as ``deselected`` in the summary)."""
    global _deselected
    _deselected += len(items)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Write the sidecar. Best-effort: never raise (must not fail the run)."""
    try:
        path = os.environ.get("PYTEST_COUNTS_FILE", "pytest-counts.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"collected": _collected, "deselected": _deselected}, f)
    except OSError:
        pass
