"""CI contract tests for P0-01 (CI false-green / CI 假绿).

These tests enforce the exit-code propagation contract on the GitHub Actions
workflow (`.github/workflows/ci.yml`) and the coverage configuration
(`pyproject.toml`):

- A failing pytest or a sub-threshold coverage gate must fail the CI job, not
  be hidden by a display-layer pipe (``| tail``) without ``set -o pipefail``.
- No ``|| true`` may silently manufacture a green step anywhere (e.g. pip-audit).
- ``fail_under = 80`` must remain a blocking coverage gate.
- The coverage step must emit a machine-readable report (``coverage.xml``).
- The ``real_llm`` lane must stay informational (excluded from blocking runs).
- Steps that execute pytest (not ``--co`` collection) must not be softened with
  ``continue-on-error: true``.
- The MCP end-to-end contract suite must be a real gate, not a silent skip:
  the blocking lane installs the official ``mcp`` SDK, fails hard when it is
  absent, and keeps the SDK an *optional* runtime dependency
  (``TestMcpContractWiring``).

Parsing is stdlib-only (no PyYAML dependency) so the ``test`` CI job — which
installs only ``requirements.txt`` plus pytest plugins — can run these checks
without adding a dependency. The parser is index-based: it walks each
``- name:`` line and extracts that step's own ``run:`` block, so a short step
name like ``Summary`` cannot collide with a longer one like ``Coverage
summary`` the way a substring search would.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YAML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# A "truncation pipe" pipes a command's output through tail/head, which can
# mask the upstream exit code unless `set -o pipefail` is enabled.
TRUNCATION_PIPE_RE = re.compile(r"\|\s*(?:tail|head)\b")


def _ci_lines() -> list[str]:
    return CI_YAML.read_text(encoding="utf-8").splitlines()


def _steps() -> list[tuple[str, str | None, str]]:
    """Return ``(name, run_script_or_None, full_block_text)`` for every step.

    Scanning is index-based: the block for a step starts at its ``- name:``
    line and runs until the next ``- `` step or a sibling job key, so each
    step's ``run:`` is extracted from its own block — no substring lookup that
    could let ``Summary`` match ``Coverage summary``.
    """
    lines = _ci_lines()
    out: list[tuple[str, str | None, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip().startswith("- name:"):
            i += 1
            continue
        name = line.strip().split("name:", 1)[1].strip()
        block_lines: list[str] = [line]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if re.match(r"^      - ", nxt) or re.match(r"^  [A-Za-z]", nxt):
                break
            block_lines.append(nxt)
            j += 1
        block = "\n".join(block_lines)
        run = _run_from_block(block_lines)
        out.append((name, run, block))
        i = j
    return out


def _run_from_block(block_lines: list[str]) -> str | None:
    """Extract the ``run:`` script from one step's block lines, or ``None``."""
    run_idx = None
    run_indent: int | None = None
    for idx, line in enumerate(block_lines):
        if line.lstrip().startswith("run:"):
            run_idx = idx
            run_indent = len(line) - len(line.lstrip())
            break
    if run_idx is None or run_indent is None:
        return None
    run_line = block_lines[run_idx]
    inline = run_line.split("run:", 1)[1].strip()
    if inline and inline not in ("|", ">", "-"):
        return inline  # single-line run command
    script: list[str] = []
    for k in range(run_idx + 1, len(block_lines)):
        line = block_lines[k]
        if line.strip() == "":
            continue
        cur_indent = len(line) - len(line.lstrip())
        if cur_indent > run_indent:
            script.append(line.strip())
        else:
            break
    return "\n".join(script) if script else None


def _step_run_exact(name: str) -> str:
    """Return the ``run:`` script of the step whose name equals ``name``
    exactly (avoids substring collisions like Summary vs Coverage summary)."""
    for n, run, _block in _steps():
        if n == name:
            if run is None:
                pytest.fail(f"Step '{name}' has no `run:` block")
            return run
    pytest.fail(f"No CI step with exact name '{name}'")


def _step_run_unique(substr: str) -> str:
    """Return the ``run:`` script of the unique step whose name contains
    ``substr``. Fails if zero or multiple steps match."""
    matches = [
        (n, run) for n, run, _b in _steps() if run is not None and substr.lower() in n.lower()
    ]
    if not matches:
        pytest.fail(f"No CI step whose name contains '{substr}' with a `run:` block")
    if len(matches) > 1:
        pytest.fail(f"'{substr}' ambiguously matches steps: {[m[0] for m in matches]}")
    return matches[0][1]


def _step_env_value(name: str, env_key: str) -> str | None:
    """Return the value of a YAML env var ``env_key`` from the step whose name
    equals ``name`` exactly. Handles inline ``key: value``, folded block scalar
    (``key: >-``), and literal block scalar (``key: |``) — the three forms YAML
    uses for action env vars. Returns ``None`` if the key is not present."""
    block = _step_block_exact(name)
    # Find the env: section
    env_idx = None
    for i, line in enumerate(block.splitlines()):
        if line.strip() == "env:":
            env_idx = i
            break
    if env_idx is None:
        return None
    lines = block.splitlines()
    env_indent = len(lines[env_idx]) - len(lines[env_idx].lstrip())
    val_indent = env_indent + 2  # entries under env: are indented 2 more
    key_line_idx = None
    for i in range(env_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        cur_indent = len(line) - len(line.lstrip())
        if cur_indent <= env_indent:
            break  # past the env section
        if cur_indent == val_indent and line.strip().startswith(env_key + ":"):
            key_line_idx = i
            break
    if key_line_idx is None:
        return None
    # Check if value is inline or folded block scalar
    key_line = lines[key_line_idx]
    _, _, after_colon = key_line.partition(":")
    trimmed = after_colon.strip()
    if trimmed == ">-":
        # Folded block scalar: collect subsequent indented lines, join with space
        parts = []
        for j in range(key_line_idx + 1, len(lines)):
            nxt = lines[j]
            if nxt.strip() == "":
                continue
            nxt_indent = len(nxt) - len(nxt.lstrip())
            if nxt_indent <= val_indent:
                break
            parts.append(nxt.strip())
        return " ".join(parts)
    return trimmed if trimmed else None


def _pytest_command_from_run(run_script: str) -> str:
    """Extract the ``python -m pytest …`` command from a ``run:`` script,
    stripping ``set -o pipefail`` (shell preamble) and ``2>&1 | tail …`` /
    ``2>&1 | head …`` (display-layer suffixes)."""
    lines = run_script.strip().splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("python -m pytest"):
            # Strip any trailing  2>&1 | tail/head …
            import re as _re

            cmd = _re.sub(r"\s*2>&1\s*\|\s*(?:tail|head)\s+-?\d+\s*$", "", stripped)
            return cmd
    raise ValueError(f"No python -m pytest line found in run script:\n{run_script}")


def _step_block_exact(name: str) -> str:
    """Return the full step block (name + env + run + …) for the step whose
    name equals ``name`` exactly. Use when a contract spans keys beyond ``run:``
    (e.g. an ``env:`` setting)."""
    for n, _run, block in _steps():
        if n == name:
            return block
    pytest.fail(f"No CI step with exact name '{name}'")


def _source_dirs_from_pyproject() -> list[str]:
    """Return the directory names listed in ``[tool.coverage.run] source`` in
    pyproject.toml. These are the business-module directories coverage must
    measure — the coverage step's ``--cov=<dir>`` list must match this set."""
    text = PYPROJECT.read_text(encoding="utf-8")
    section = re.search(r"\[tool\.coverage\.run\](.*?)(?=\n\[|\Z)", text, re.S)
    if not section:
        return []
    # Match quoted entries inside the source = [ ... ] list
    return [m.group(1) for m in re.finditer(r'["\']([a-z_]+)["\']', section.group(1))]


class TestExitCodePropagation:
    def test_coverage_step_preserves_pytest_exit_code(self):
        """The blocking coverage step must not pipe pytest through tail/head
        unless ``set -o pipefail`` propagates the non-zero exit code."""
        run = _step_run_exact("Run coverage report")
        if TRUNCATION_PIPE_RE.search(run):
            assert "set -o pipefail" in run, (
                "Coverage step pipes pytest output through tail/head without "
                "`set -o pipefail`; a failing test or sub-threshold coverage "
                "would be reported as success (false green)."
            )

    def test_no_step_pipes_tail_without_pipefail(self):
        """No step anywhere may swallow an exit code via a truncation pipe
        unless ``set -o pipefail`` is enabled for that step."""
        offenders = [
            name
            for name, run, _block in _steps()
            if run is not None and TRUNCATION_PIPE_RE.search(run) and "set -o pipefail" not in run
        ]
        assert not offenders, (
            "These steps pipe through tail/head without `set -o pipefail`, "
            f"swallowing exit codes: {offenders}"
        )

    def test_pip_audit_step_does_not_fake_green(self):
        """The pip-audit step must not append ``|| true`` (P0-01 Required
        Change #6). A blanket guard in ``test_no_run_step_uses_or_true``
        extends this to every step."""
        run = _step_run_unique("pip-audit")
        assert "|| true" not in run, (
            "pip-audit step uses `|| true`, manufacturing a false green when a "
            "dependency vulnerability is found."
        )

    def test_no_run_step_uses_or_true(self):
        """No ``run:`` step may use ``|| true`` to manufacture a green exit
        code — catches pytest, pip-audit, bandit, mypy, or any future step."""
        offenders = [name for name, run, _b in _steps() if run is not None and "|| true" in run]
        assert not offenders, f"Steps use `|| true` to fake a green exit code: {offenders}"

    def test_blocking_test_steps_are_not_softened(self):
        """Steps that execute pytest (not ``--co`` collection) must not be
        softened with ``continue-on-error: true``. Identified by content so a
        step rename cannot silently drop it from the check."""
        offenders = []
        for name, run, block in _steps():
            if run is None or "python -m pytest" not in run or "--co" in run:
                continue  # not a blocking test-execution step
            if re.search(r"continue-on-error\s*:\s*true", block):
                offenders.append(name)
        assert (
            not offenders
        ), f"Test-execution steps softened with continue-on-error: true: {offenders}"


class TestCoverageGate:
    def test_fail_under_threshold_is_80(self):
        """pyproject.toml must keep ``fail_under = 80`` as a blocking gate in
        ``[tool.coverage.report]`` — the section coverage reads for exit-code
        enforcement (verified empirically: coverage 7.4.4 emits
        ``CoverageWarning: Unrecognized option '[tool.coverage.run] fail_under='``
        if it is placed in ``[tool.coverage.run]``, and ignores it). The gate
        was confirmed working with only ``[tool.coverage.report]`` present:
        coverage 64.52% < 80 → pytest exit 1."""
        text = PYPROJECT.read_text(encoding="utf-8")
        section = re.search(r"\[tool\.coverage\.report\](.*?)(?=\n\[|\Z)", text, re.S)
        assert section, "No [tool.coverage.report] section in pyproject.toml"
        m = re.search(r"fail_under\s*=\s*(\d+)", section.group(1))
        assert m, "No fail_under in [tool.coverage.report]"
        assert int(m.group(1)) == 80

    def test_fail_under_not_in_run_section(self):
        """``fail_under`` must NOT be in ``[tool.coverage.run]`` — coverage 7.4.4
        emits ``CoverageWarning: Unrecognized option '[tool.coverage.run]
        fail_under='`` when it is, introducing config noise (a new warning, not
        pre-existing). The valid, recognized location is
        ``[tool.coverage.report]`` (see test_fail_under_threshold_is_80)."""
        text = PYPROJECT.read_text(encoding="utf-8")
        run_section = re.search(r"\[tool\.coverage\.run\](.*?)(?=\n\[|\Z)", text, re.S)
        assert run_section, "No [tool.coverage.run] section in pyproject.toml"
        assert not re.search(r"fail_under\s*=", run_section.group(1)), (
            "fail_under is present in [tool.coverage.run]; coverage 7.4.4 treats "
            "this as an unrecognized option and emits a CoverageWarning. Move it "
            "to [tool.coverage.report] only."
        )

    def test_coverage_step_emits_machine_readable_xml(self):
        """The coverage step must emit ``coverage.xml`` for machine-readable
        coverage data (P0-01 Required Change #5)."""
        run = _step_run_exact("Run coverage report")
        assert "--cov-report=xml" in run


class TestMachineReadableReports:
    """AC4: the report must distinguish passed, collected, skipped, failed
    AND coverage — all machine-readable, not just in the (tail-truncated) log.
    JUnit gives passed/failed/skipped/executed; a local pytest plugin gives
    collected/deselected (which JUnit lacks); coverage.xml gives coverage.
    """

    def test_coverage_step_emits_junit_xml(self):
        """The coverage step must emit a JUnit XML test report
        (``--junit-xml``) so passed/failed/skipped/executed are queryable
        artifacts, not buried in truncated terminal output."""
        run = _step_run_exact("Run coverage report")
        assert "--junit-xml" in run, (
            "Coverage step does not emit --junit-xml; test counts "
            "(passed/failed/skipped/executed) are only in the tail-truncated "
            "log, not a machine-readable report (AC4)."
        )

    def test_coverage_step_loads_counts_plugin(self):
        """The coverage step must load the ``pytest_counts`` plugin (and set
        ``PYTEST_COUNTS_FILE``) so ``collected``/``deselected`` are written to
        a machine-readable sidecar — the counts JUnit XML does NOT record
        (AC4: report must distinguish ``collected``). The ``PYTEST_COUNTS_FILE``
        env var is checked against the full step block (env: lives outside the
        run: script)."""
        block = _step_block_exact("Run coverage report")
        assert "-p pytest_counts" in block, (
            "Coverage step does not load -p pytest_counts; collected/deselected "
            "counts are not captured in a machine-readable sidecar (AC4)."
        )
        assert "PYTEST_COUNTS_FILE" in block, "Coverage step does not set PYTEST_COUNTS_FILE (AC4)."

    def test_summary_step_calls_ci_summary_with_sidecar(self):
        """The summary step must call ``ci_summary`` with the JUnit, coverage,
        AND counts sidecar paths, so the surfaced summary includes all of
        passed/collected/deselected/skipped/failed + coverage (AC4). The
        helper's behavior is unit-tested in ``tests/unit/test_ci_summary.py``.
        """
        run = _step_run_exact("Test and coverage summary")
        assert "ci_summary" in run, "Summary step does not call the ci_summary helper (AC4)."
        assert "pytest-counts.json" in run, (
            "Summary step does not pass the pytest-counts.json sidecar to "
            "ci_summary; collected/deselected would not be surfaced (AC4)."
        )

    def test_coverage_step_records_invocation_for_provenance(self):
        """AC8 reproducibility: the coverage step must record the exact pytest
        command in a ``PYTEST_INVOCATION`` env var so ``ci_summary`` can surface
        it in the provenance line — a coverage figure is only meaningful if the
        command that produced it is recorded. Mirrors the existing
        ``PYTEST_COUNTS_FILE`` assertion (env lives outside the ``run:`` script,
        so we check the full step block)."""
        block = _step_block_exact("Run coverage report")
        assert "PYTEST_INVOCATION" in block, (
            "Coverage step does not set PYTEST_INVOCATION; the pytest command "
            "that produced the coverage figure is not recorded in provenance "
            "(AC8 reproducibility)."
        )


class TestLaneClassification:
    def test_real_llm_lane_excluded_from_blocking_gate(self):
        """``real_llm`` tests require a real API key and must be informational
        — excluded from the blocking coverage gate."""
        run = _step_run_exact("Run coverage report")
        assert "not real_llm" in run


class TestFalseGreenMechanism:
    """Behavioral proof of the false-green mechanism and the pipefail fix.

    GitHub Actions enables ``pipefail`` by default for its bash shell, so the
    bug is latent under the default shell. The contract tests above require
    ``pipefail`` to be EXPLICIT so the workflow is not fragile to a shell
    override (``sh`` / ``cmd`` / ``pwsh``), a ``defaults.run.shell`` setting, or
    execution outside GitHub Actions. These behavioral tests prove the
    underlying shell mechanism: without pipefail a pipe swallows failures; with
    pipefail it propagates them.
    """

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
    def test_tail_swallows_failure_without_pipefail(self):
        bash = shutil.which("bash")
        r = subprocess.run(
            "false 2>&1 | tail -1",
            shell=True,
            executable=bash,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, (
            "without pipefail, a failing command piped through tail exits 0 "
            "(false green) — the latent bug P0-01 guards against"
        )

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
    def test_pipefail_propagates_failure_through_tail(self):
        bash = shutil.which("bash")
        r = subprocess.run(
            "set -o pipefail; false 2>&1 | tail -1",
            shell=True,
            executable=bash,
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0, (
            "with `set -o pipefail`, a failing command piped through tail must "
            "propagate its non-zero exit code (the fix)"
        )


class TestMarkerRegistration:
    """Every ``pytest.mark.<name>`` used in ``tests/`` must be a registered
    marker — either a pytest builtin / plugin marker (surfaced by ``pytest
    --markers``) or declared in ``pyproject.toml``'s ``markers`` list.

    An unregistered custom marker emits ``PytestUnknownMarkWarning`` — the
    "pytest.mark.unit 未注册" noise flagged in the P0-01 acceptance report. This
    scan is mutation-safe drift protection: the moment someone adds an
    unregistered custom marker to a test, CI goes red.

    Builtins (``asyncio`` from pytest-asyncio, ``skipif``, ``parametrize``,
    ``xfail``, …) are NOT hardcoded here — ``pytest --markers`` is the source of
    truth, and unregistered custom markers do NOT appear in its output (they
    only warn), so they are correctly flagged.
    """

    MARKER_USAGE_RE = re.compile(r"pytest\.mark\.([a-z_]+)\b")
    # A real marker *use* is either a decorator ``@pytest.mark.X`` or a call
    # ``pytest.mark.X(...)`` — NOT prose inside comments/docstrings (which is
    # how ``pytest.mark.integration`` appears in this very test file). We strip
    # ``#`` comments first, then require the ``@`` decorator prefix OR a
    # following ``(`` call so a bare mention in a docstring is not counted.
    DECORATOR_MARKER_RE = re.compile(r"@pytest\.mark\.([a-z_]+)\b")
    CALL_MARKER_RE = re.compile(r"\bpytest\.mark\.([a-z_]+)\s*\(")

    @staticmethod
    def _registered_markers() -> set[str]:
        """Return the set of marker names pytest reports as registered (builtins
        + ini markers + plugin markers, e.g. ``asyncio``). Runs ``pytest
        --markers`` in a subprocess — the authoritative source."""
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "--markers"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        # `pytest --markers` lists each as "@pytest.mark.name: description".
        return {
            m.group(1)
            for line in r.stdout.splitlines()
            if (m := re.match(r"@pytest\.mark\.([a-z_]+)\b", line.strip()))
        }

    @staticmethod
    def _ini_markers() -> set[str]:
        """Return the marker names declared in pyproject.toml's ``markers``
        list (the custom ones this repo registers)."""
        text = PYPROJECT.read_text(encoding="utf-8")
        section = re.search(r"\[tool\.pytest\.ini_options\](.*?)(?=\n\[|\Z)", text, re.S)
        if not section:
            return set()
        # Match `    "name: ..."` or `'name: ...'` entries inside the list.
        return {m.group(1) for m in re.finditer(r'["\']([a-z_]+):', section.group(1))}

    @staticmethod
    def _used_markers() -> set[str]:
        """Return every marker name actually *used* as a decorator or call in
        ``tests/**/*.py`` (excluding worktrees and venvs). Prose mentions in
        comments/docstrings are stripped so they don't false-positive."""
        used: set[str] = set()
        for path in REPO_ROOT.glob("tests/**/*.py"):
            # Skip nested worktree/venv copies that may sit under tests/.
            if "/.worktrees/" in str(path) or "/.venv/" in str(path):
                continue
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                # Drop ``#`` comments (a docstring line like
                # ``# ... pytest.mark.integration ...`` is prose, not a use).
                line = raw_line.split("#", 1)[0]
                for m in TestMarkerRegistration.DECORATOR_MARKER_RE.finditer(line):
                    used.add(m.group(1))
                for m in TestMarkerRegistration.CALL_MARKER_RE.finditer(line):
                    used.add(m.group(1))
        return used

    def test_all_markers_used_in_tests_are_registered(self):
        # Arrange: union of pytest-registered (builtins + plugins) and the
        # repo's own ini-declared markers is the full set of "known" markers.
        known = self._registered_markers() | self._ini_markers()
        used = self._used_markers()

        # Act / Assert: every marker used in tests/ must be known.
        unregistered = sorted(used - known)
        assert not unregistered, (
            "These pytest markers are used in tests/ but not registered (neither "
            "a pytest builtin/plugin marker nor declared in pyproject.toml "
            "[tool.pytest.ini_options].markers) — they emit "
            f"PytestUnknownMarkWarning: {unregistered}"
        )


class TestArtifactHygiene:
    """CI-generated reports (``coverage.xml``, ``pytest-report.xml``,
    ``pytest-counts.json``) are per-run build artifacts, not source. They must
    be gitignored and never committed — a tracked, stale ``coverage.xml``
    (committed in 68e08c9, later found carrying ``line-rate=0.768``, a third
    number that matched neither 78.36 nor 78.72) was exactly the kind of
    coverage-number pollution P0-01 exists to prevent.
    """

    ARTIFACTS = ("coverage.xml", "pytest-report.xml", "pytest-counts.json")

    def test_ci_artifacts_are_gitignored(self):
        # Arrange
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

        # Act / Assert: each artifact pattern appears as its own gitignore line
        # (not as a substring of another path) so it is actually ignored.
        missing = [
            name
            for name in self.ARTIFACTS
            if not any(line.strip() == name for line in gitignore.splitlines())
        ]
        assert not missing, (
            "These CI artifacts are not listed in .gitignore and could be "
            f"committed as stale per-run data: {missing}"
        )

    def test_ci_artifacts_are_not_tracked(self):
        # Arrange: `git ls-files` lists tracked paths.
        r = subprocess.run(
            ["git", "ls-files"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert r.returncode == 0, f"git ls-files failed: {r.stderr}"
        tracked = {line.strip() for line in r.stdout.splitlines() if line.strip()}

        # Act / Assert: no CI artifact is tracked.
        committed = [name for name in self.ARTIFACTS if name in tracked]
        assert not committed, (
            "These CI artifacts are tracked in git (committed as stale per-run "
            f"data): {committed} — run `git rm --cached` on them."
        )


class TestToolchainReproducibility:
    """AC8: the coverage toolchain must be fully pinned so the coverage number
    is reproducible. The CI install step (``Install dependencies``) must pin all
    four packages that produce the coverage figure: pytest, pytest-asyncio,
    pytest-cov, and coverage. ``importlib.metadata`` versions of these four
    packages appear in ``ci_summary.py``'s provenance line.

    The 78.08% / 78.65% / 78.72% spread previously observed was caused by
    pytest-cov and coverage being unpinned — CI had one version, local env had
    another. A different pytest version also changes test collection (deselected
    count differs), which changes the coverage denominator.
    """

    def test_install_step_pins_full_coverage_toolchain(self):
        """The ``Install dependencies`` step must pin all four packages that
        produce the coverage number, not just two of them."""
        run = _step_run_exact("Install dependencies")
        assert run is not None, "Install dependencies step has no run: block"
        pins = {
            "pytest": "pytest==" in run,
            "pytest-asyncio": "pytest-asyncio==" in run,
            "pytest-cov": "pytest-cov==" in run,
            "coverage": "coverage==" in run,
        }
        missing = [name for name, pinned in pins.items() if not pinned]
        assert not missing, (
            "These coverage-toolchain packages are not pinned to a specific "
            f"version in the CI install step: {missing}. Without a full pin "
            "the coverage number is irreproducible across environments "
            "(AC8)."
        )

    def test_invocation_matches_run_command(self):
        """The ``PYTEST_INVOCATION`` env var in the ``Run coverage report`` step
        must be byte-identical to the ``python -m pytest …`` command in the
        ``run:`` script (minus the ``2>&1 | tail`` display layer, which is not
        part of the pytest invocation). Drift between the two is a provenance
        consistency failure — the recorded invocation would not match the actual
        command, making the coverage figure unverifiable (AC8)."""
        run = _step_run_exact("Run coverage report")
        env_val = _step_env_value("Run coverage report", "PYTEST_INVOCATION")
        assert env_val is not None, "PYTEST_INVOCATION env var is not set"
        cmd = _pytest_command_from_run(run)
        assert env_val == cmd, (
            "PYTEST_INVOCATION env var does not match the actual pytest command "
            f"in the run: script.\n\nEnv var:\n{env_val}\n\nRun command:\n{cmd}\n\n"
            "These must stay in sync (AC8 reproducibility)."
        )

    def test_cov_command_lists_every_source_dir(self):
        """The coverage step must pass ``--cov=<dir>`` for EVERY directory in
        ``pyproject.toml``'s ``[tool.coverage.run] source`` list. This is the
        ONLY form proven to scope coverage to business modules: ``--cov`` (no
        path) and ``--cov=.`` both cause coverage 7.4.4 to measure every
        imported file (sitecustomize.py, chardet/*, _pytest/*, scripts/*),
        polluting the coverage number. The source list is the single source of
        truth — if a new module dir is added to ``source`` but not to the CI
        command (or vice versa), this test fails (AC8 scope correctness)."""
        run = _step_run_exact("Run coverage report")
        source_dirs = _source_dirs_from_pyproject()
        assert source_dirs, (
            "No source dirs found in [tool.coverage.run] source — the coverage "
            "scope contract cannot be verified without a source list."
        )
        missing = [d for d in source_dirs if f"--cov={d}" not in run]
        assert not missing, (
            "These source directories from pyproject.toml's "
            f"[tool.coverage.run] source are not passed as --cov=<dir> in the "
            f"coverage step: {missing}. Without explicit --cov=<dir> for every "
            "source dir, coverage 7.4.4 measures third-party files (AC8 scope)."
        )

    def test_cov_command_has_no_pathless_or_dot_cov(self):
        """The coverage step must NOT use ``--cov`` (pathless) or ``--cov=.``,
        both of which cause coverage 7.4.4 to measure every imported file
        (system/third-party), polluting the coverage number. Only explicit
        ``--cov=<dir>`` reliably scopes to business modules (AC8)."""
        run = _step_run_exact("Run coverage report")
        # Pathless --cov appears as a standalone token (not --cov=...).
        # Match `--cov` followed by whitespace or end, NOT `--cov=`.
        pathless = re.search(r"(?:^|\s)--cov(?=\s|$)", run)
        assert not pathless, (
            "Coverage step uses pathless `--cov`; coverage 7.4.4 then measures "
            "every imported file (sitecustomize, chardet, _pytest, scripts/*). "
            "Use explicit --cov=<dir> for each source directory."
        )
        assert "--cov=." not in run, (
            "Coverage step uses --cov=. which measures the whole tree incl. "
            "tests/. Use explicit --cov=<dir> for each source directory."
        )


class TestMcpContractWiring:
    """The MCP e2e contract suite must be a real gate in the blocking `test`
    job, not a silent skip.

    ``tests/integration/test_mcp_contract_e2e.py`` does
    ``pytest.importorskip("mcp", …)``. The official SDK is an *optional*
    runtime dependency (requirements-optional.txt, ``MCP_ENABLED`` off by
    default), so the test is legitimately skippable for a local developer. But
    the `test` job is a blocking lane: if the SDK is absent there, the suite
    skips and the lane reports PASS having verified nothing about MCP. These
    tests lock the three properties that prevent that false green:

    1. the blocking lane installs the official SDK;
    2. its absence is a hard FAIL (preflight import), not a skip;
    3. the CI constraint matches the declared optional policy (no drift).

    They deliberately do NOT require `mcp` in requirements.txt: the app's
    runtime dependency policy must stay unchanged and optional.
    """

    CI_MCP_STEP = "Install MCP contract-test dependency (official SDK)"
    PREFLIGHT_STEP = "Verify MCP SDK importable (contract suite must not skip)"
    GATE_STEP = "MCP end-to-end contract evidence (no silent skip)"
    REQ_OPT = REPO_ROOT / "requirements-optional.txt"
    REQ = REPO_ROOT / "requirements.txt"

    def _declared_mcp_spec(self, path: Path) -> str | None:
        """Return the version specifier declared for the ``mcp`` distribution
        in a requirements file (e.g. ``mcp>=2.0.0``), or ``None`` if absent."""
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            if re.match(r"^mcp\s*(?:\[.*\])?\s*(?:[<>!=~]=?|===)", line):
                return re.sub(r"\s+", " ", line)
        return None

    def test_blocking_lane_installs_official_mcp_sdk(self):
        """The blocking `test` job must install the official `mcp` SDK, because
        it is the client side of the MCP end-to-end contract."""
        run = _step_run_exact(self.CI_MCP_STEP)
        assert re.search(r"pip install\s+[\"']?mcp\b", run), (
            f"The '{self.CI_MCP_STEP}' step does not install the official mcp "
            f"SDK:\n{run}\nWithout it the MCP contract suite importorskips and "
            "this blocking lane goes green without verifying any MCP behaviour."
        )

    def test_mcp_contract_wiring_matches_optional_policy(self):
        """The constraint CI installs must equal the one declared in
        requirements-optional.txt. Duplicated constraints drift silently, and
        the drift is invisible until the blocking lane starts failing (or,
        worse, starts skipping) on an unrelated change."""
        run = _step_run_exact(self.CI_MCP_STEP)
        ci_specs = {
            re.sub(r"\s+", " ", m.replace('"', "").replace("'", "").strip())
            for m in re.findall(r"[\"']mcp[^\"']*[\"']", run)
        }
        assert (
            ci_specs
        ), f"No quoted mcp requirement specifier found in the '{self.CI_MCP_STEP}' step:\n{run}"
        declared = self._declared_mcp_spec(self.REQ_OPT)
        assert declared is not None, (
            "requirements-optional.txt no longer declares the official mcp SDK. "
            "It is an optional runtime dependency with a delayed import in "
            "tools/mcp_adapter.py; removing the declaration would let the "
            "dependency go undeclared and silently degrade."
        )
        drift = sorted(s for s in ci_specs if s.lower() != declared.lower())
        assert not drift, (
            f"The mcp constraint installed in CI {sorted(ci_specs)} has drifted "
            f"from the declared optional policy '{declared}' in "
            "requirements-optional.txt. They must be identical."
        )

    def test_mcp_stays_an_optional_runtime_dependency(self):
        """The CI fix must not promote `mcp` into the runtime requirements.
        `MCP_ENABLED` is off by default and the adapter imports the SDK lazily,
        so making it mandatory would change the app's dependency policy."""
        assert self._declared_mcp_spec(self.REQ) is None, (
            "requirements.txt now declares the official mcp SDK. The MCP tool "
            "adapter is an off-by-default read-only capability whose SDK import "
            "is delayed; requiring it at runtime would change the app dependency "
            "policy. It belongs in requirements-optional.txt."
        )

    def test_missing_sdk_fails_the_blocking_lane(self):
        """A preflight step must import the SDK and exit non-zero when it is
        missing, so the missing dependency surfaces as a FAIL with a
        self-explanatory message instead of a skip."""
        run = _step_run_exact(self.PREFLIGHT_STEP)
        assert "import mcp" in run, (
            f"The '{self.PREFLIGHT_STEP}' step does not import the mcp SDK, so it "
            f"cannot detect its absence:\n{run}"
        )
        assert re.search(r"SystemExit\(1\)|sys\.exit\(1\)|exit 1", run), (
            f"The '{self.PREFLIGHT_STEP}' step does not fail on a missing SDK "
            f"(no non-zero exit):\n{run}\nA missing SDK must FAIL the job; "
            "returning 0 would re-open the silent-skip false green."
        )
        assert "|| true" not in run, (
            f"The '{self.PREFLIGHT_STEP}' step swallows failure with `|| true`, "
            "which would turn a missing SDK into a green build."
        )
        block = _step_block_exact(self.PREFLIGHT_STEP)
        assert "continue-on-error" not in block, (
            f"The '{self.PREFLIGHT_STEP}' step sets continue-on-error, so a "
            "missing SDK would no longer fail the job."
        )

    def test_contract_gate_fails_on_any_skip(self):
        """The blocking gate must reuse scripts/verify_mcp_contract.py, which
        treats skipped tests and uncovered required scenarios as FAIL. A bare
        `pytest` invocation cannot express that: its exit code is 0 when the
        whole module importorskips."""
        run = _step_run_exact(self.GATE_STEP)
        assert "verify_mcp_contract.py" in run, (
            f"The '{self.GATE_STEP}' step does not run the MCP contract evidence "
            f"entry point:\n{run}\nThat script is what turns a skip into a FAIL; "
            "a plain pytest run reports success when the suite never ran."
        )
        assert "continue-on-error" not in _step_block_exact(self.GATE_STEP), (
            f"The '{self.GATE_STEP}' step sets continue-on-error, so a skipped or "
            "uncovered contract would not fail the job."
        )

    def test_mcp_gate_runs_before_the_pytest_lanes(self):
        """The SDK install / preflight must precede the pytest steps. If the
        preflight runs last, the coverage and integration lanes have already
        reported green with the MCP suite silently skipped."""
        names = [n for n, _run, _b in _steps()]
        pre_idx = names.index(self.PREFLIGHT_STEP)
        for earlier_step in ("Run integration tests", "Run coverage report"):
            assert earlier_step in names, (
                f"Expected a '{earlier_step}' step in the blocking lane; the MCP "
                "wiring contract cannot be verified against a workflow that does "
                "not run it."
            )
            assert pre_idx < names.index(earlier_step), (
                f"'{self.PREFLIGHT_STEP}' runs after '{earlier_step}'. The SDK "
                "preflight must run first, otherwise those lanes already reported "
                "green with the MCP contract suite skipped."
            )
