"""Regression tests for the crash-recovery kill gate's predicate.

The recorded Gate 7 flake was **not** a runtime defect: the test's SIGKILL was
gated on ``count(*) >= 1`` over the thread's checkpoints, which the *input*
checkpoint already satisfies. Because LangGraph commits super-step checkpoints
on a background executor it does not await, a SIGKILL inside that window
destroys the in-flight commit, and recovery then correctly restarts from the
input checkpoint and re-runs ``first`` — breaking ``first_after == 1`` and
``recovered >= 1``.

The fix is to gate on the invariant the assertions actually depend on. These
tests pin the *predicate* so a future edit cannot silently weaken it back to a
row count, and they pin the two shapes of checkpoint history that made the
distinction matter.

This module needs no PostgreSQL, Redis or worker: the predicate is a pure
function of the committed rows. See Issue #30 and
``scripts/probe_crash_kill_gate.py`` for the reproduction evidence.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TEST_PATH = (
    Path(__file__).resolve().parents[1] / "integration" / "runtime" / "test_worker_checkpoint_recovery.py"
)


def _load_module():
    """Import the test module by path.

    Loading it by path (rather than by package name) keeps this unit test out of
    the ``runtime`` name-shadowing problem documented in
    ``tests/integration/runtime/conftest.py``: that directory deliberately has no
    ``__init__.py`` so it cannot shadow the application's ``runtime`` package.
    """
    spec = importlib.util.spec_from_file_location("_ckpt_recovery_under_test", _TEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_module()
first_superstep_durable = _MODULE.first_superstep_durable
DURABLE = _MODULE._DURABLE_FIRST_CHANNEL


def test_durable_channel_is_the_second_branch_not_the_first():
    """Pin *which* channel is the durability marker.

    ``branch:to:first`` is written by the START -> first edge, i.e. before
    ``first`` has produced anything, so it cannot stand in for "first's result
    is committed". If someone swaps the constant, this fails.
    """
    assert DURABLE == "branch:to:second"


def test_empty_history_is_not_durable():
    """No checkpoint at all must never satisfy the gate."""
    assert first_superstep_durable([]) is False


def test_input_checkpoint_only_is_not_durable():
    """The exact state that caused the flake.

    Before any super-step completes, the only committed row carries the input
    state and the START -> first branch. A row count is >= 1 here, so the old
    gate passed; the invariant the test needs was still false.
    """
    rows = [
        {
            "checkpoint_id": "ckpt-1",
            "channels": ["customer_query", "response", "resumed", "stage", "branch:to:first"],
        }
    ]
    assert first_superstep_durable(rows) is False


def test_durable_once_first_result_is_committed():
    rows = [
        {
            "checkpoint_id": "ckpt-1",
            "channels": ["customer_query", "response", "resumed", "stage", "branch:to:first"],
        },
        {
            "checkpoint_id": "ckpt-2",
            "channels": [
                "customer_query",
                "response",
                "resumed",
                "stage",
                "branch:to:first",
                DURABLE,
            ],
        },
    ]
    assert first_superstep_durable(rows) is True


def test_only_the_newest_committed_row_decides():
    """A stale durable row must not vouch for the current state.

    The gate exists to protect a *kill*, so it must describe the newest committed
    checkpoint. If a later commit somehow lacks the marker, the gate has to
    report not-durable rather than being reassured by an earlier row.
    """
    rows = [
        {"checkpoint_id": "ckpt-1", "channels": ["customer_query", DURABLE]},
        {"checkpoint_id": "ckpt-2", "channels": ["customer_query", "response"]},
    ]
    assert first_superstep_durable(rows) is False


def test_missing_channels_key_is_treated_as_not_durable():
    """Defensive: a row without a ``channels`` list must not raise or pass."""
    assert first_superstep_durable([{"checkpoint_id": "ckpt-1"}]) is False
    assert first_superstep_durable([{"checkpoint_id": "ckpt-1", "channels": None}]) is False


def test_gate_is_not_a_row_count():
    """Directly encode the defect being fixed.

    If this test ever needs changing, the kill gate has been weakened back to
    counting rows and the recorded flake can return.
    """
    input_only = [{"checkpoint_id": "ckpt-1", "channels": ["customer_query"]}]
    assert len(input_only) >= 1  # the old predicate would have passed here
    assert first_superstep_durable(input_only) is False  # the invariant predicate does not


@pytest.mark.parametrize(
    ("channels", "expected"),
    [
        (["a", DURABLE], True),
        (["a", "b"], False),
        ([DURABLE.upper()], False),
        (["x" + DURABLE], False),
        (["prefix" + DURABLE + "suffix"], False),
    ],
)
def test_marker_match_is_exact_not_substring(channels, expected):
    """Exact key membership, so a similarly named channel cannot spoof the gate."""
    rows = [{"checkpoint_id": "ckpt-1", "channels": channels}]
    assert first_superstep_durable(rows) is expected
