"""Repository contract guards (2026-10-01 repository truth alignment).

These tests enforce whole-repository contracts that are broader than the
documentation-only guards in ``test_doc_consistency.py`` and the CI-workflow
guards in ``test_ci_contract.py``:

- Product version metadata is single-sourced from ``core/config.py::VERSION``
  (``pyproject.toml`` / ``package.json`` / ``package-lock.json`` must agree).
- The dev dependency contract keeps the pytest 8 / pytest-asyncio >= 0.23.4
  compatibility floor (the PR #21 follow-up).
- The deployment dependency contract is unambiguous: ``requirements.txt`` +
  ``Dockerfile``, never the stale ``requirements-lock.txt`` snapshot.
- Historical documents cannot reclaim current-truth authority, and the
  canonical current-truth doc stays unique.

Stdlib-only so the pinned LANE A CI test job (which installs only
``requirements.txt`` plus pytest plugins) can run these checks.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _default_runtime_version() -> str:
    text = _read("core/config.py")
    m = re.search(
        r'VERSION\s*=\s*os\.getenv\(\s*["\']APP_VERSION["\']\s*,\s*["\']([^"\']+)["\']',
        text,
    )
    assert m, "core/config.py VERSION default not found"
    return m.group(1)


def _pyproject_version() -> str:
    text = _read("pyproject.toml")
    m = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', text)
    assert m, "pyproject.toml version not found"
    return m.group(1)


def _requirement_spec(text: str, name: str) -> str:
    m = re.search(rf"(?m)^{re.escape(name)}\s*([<>=!~].*)$", text)
    assert m, f"requirement {name!r} not found"
    return m.group(1).strip()


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", value))


class TestVersionContract:
    def test_runtime_version_is_6_3(self):
        assert _default_runtime_version() == "6.3"

    def test_all_product_metadata_versions_agree(self):
        runtime = _default_runtime_version()
        pkg = json.loads(_read("package.json"))
        lock = json.loads(_read("package-lock.json"))
        versions = {
            "core/config.py": runtime,
            "pyproject.toml": _pyproject_version(),
            "package.json": pkg["version"],
            "package-lock.json": lock["version"],
            "package-lock.json[packages['']]": lock["packages"][""]["version"],
        }
        assert len(set(versions.values())) == 1, f"version metadata drift: {versions}"
        assert runtime == "6.3"

    def test_package_description_has_no_stale_current_version(self):
        description = json.loads(_read("package.json"))["description"]
        assert "v6.1" not in description, (
            "package.json description still advertises v6.1 as the current "
            f"product version: {description!r}"
        )
        assert "6.3" in description

    def test_historical_versions_are_preserved(self):
        """A blind global version replace would delete history: the README's
        dated v6.0/v5.4 sections and the release notes must survive."""
        readme = _read("README.md")
        assert "v6.0 核心更新" in readme
        assert "v5.4" in readme
        assert (REPO_ROOT / "docs/reports/releases/release-notes-v6.0.md").exists()
        changelog = _read("docs/reports/releases/changelog.md")
        assert "## v6.2" in changelog and "## v6.3" in changelog


class TestDependencyContract:
    def test_dev_pytest_asyncio_floor_is_0234(self):
        text = _read("requirements-dev.txt")
        pytest_spec = _requirement_spec(text, "pytest").replace(" ", "")
        pa_spec = _requirement_spec(text, "pytest-asyncio")
        assert ">=8" in pytest_spec, f"pytest 8 development lane missing: {pytest_spec}"
        m = re.search(r">=\s*([0-9][0-9.]*)", pa_spec)
        assert m, f"no lower bound in pytest-asyncio spec: {pa_spec}"
        assert _version_tuple(m.group(1)) >= (0, 23, 4), (
            "pytest-asyncio floor must be >=0.23.4 for pytest>=8 "
            f"(FixtureDef.unittest incompatibility); got {pa_spec!r}"
        )

    def test_dev_comment_does_not_claim_023_floor(self):
        text = _read("requirements-dev.txt")
        assert "pytest-asyncio floor 0.23:" not in text, (
            "requirements-dev.txt still describes the superseded >=0.23 floor; "
            "the supported floor is >=0.23.4"
        )
        assert "0.23.4" in text

    def test_docker_installs_requirements_txt_not_stale_lock(self):
        dockerfile = _read("Dockerfile")
        assert "-r requirements.txt" in dockerfile
        assert "-r requirements-lock.txt" not in dockerfile, (
            "Dockerfile installs requirements-lock.txt, which is a "
            "non-authoritative snapshot; the deployment contract is "
            "requirements.txt."
        )
        assert not re.search(r"(?m)^\s*COPY\s+(?:\./)?requirements-lock\.txt", dockerfile)

    def test_stale_lock_is_labeled_non_authoritative(self):
        head = "\n".join(_read("requirements-lock.txt").splitlines()[:40])
        assert "SUPERSEDED" in head
        assert "NON-AUTHORITATIVE" in head
        assert "pip install -r requirements-lock.txt" not in head, (
            "requirements-lock.txt still advertises itself as an install entry"
        )
        assert not re.search(r"用法（生产部署）", head), (
            "requirements-lock.txt still claims a production deployment role"
        )

    def test_makefile_lock_is_not_production_authority(self):
        makefile = _read("Makefile")
        m = re.search(r"(?ms)^lock:.*?(?=\n#|\n[A-Za-z0-9_.-]+:)", makefile)
        assert m, "make lock target not found"
        block = m.group(0)
        assert "非权威" in block, "make lock no longer states it is non-authoritative"
        assert "生成依赖锁定文件" not in block
        # `make lock` must not clobber the frozen historical snapshot (whose
        # SUPERSEDED header is guarded here); it writes an untracked local file.
        assert "requirements-lock.local.txt" in block
        assert not re.search(r">\s*requirements-lock\.txt\b", block), (
            "make lock overwrites requirements-lock.txt, destroying its non-authoritative header"
        )

    def test_local_lock_snapshot_is_gitignored(self):
        gitignore = _read(".gitignore")
        assert any(
            line.strip() == "requirements-lock.local.txt" for line in gitignore.splitlines()
        ), "requirements-lock.local.txt is not gitignored"

    def test_makefile_declares_layered_dependency_contract(self):
        makefile = _read("Makefile")
        assert "唯一依赖契约" not in makefile, (
            "Makefile still claims requirements.txt is the single dependency "
            "contract; state the Deployment / CI Lane A / Dev Lane B layers"
        )
        for token in (
            "Deployment",
            "requirements.txt + Dockerfile",
            "CI Lane A",
            "Dev compatibility Lane B",
            "requirements-dev.txt",
            "HISTORICAL / NON-AUTHORITATIVE",
            "requirements-lock.local.txt",
        ):
            assert token in makefile, f"Makefile dependency contract missing: {token}"


class TestHistoricalAuthority:
    def test_codex_spec_no_longer_claims_unique_spec(self):
        text = _read("docs/audit/CODEX_PROJECT_REMEDIATION_SPEC.md")
        assert "SUPERSEDED" in text[:2500]
        assert "本文件是项目整改的唯一执行规格" not in text, (
            "CODEX_PROJECT_REMEDIATION_SPEC.md reclaimed its superseded '唯一执行规格' authority"
        )
        assert "current-state.md" in text[:2500]
        assert "production-evidence.md" in text[:2500]

    def test_superpowers_docs_are_banner_marked(self):
        docs = sorted((REPO_ROOT / "docs/superpowers").rglob("*.md"))
        assert docs, "docs/superpowers has no documents to check"
        missing = [
            str(p.relative_to(REPO_ROOT))
            for p in docs
            if "HISTORICAL" not in p.read_text(encoding="utf-8")[:600]
        ]
        assert not missing, f"superpowers docs missing a historical banner: {missing}"


# Strong-copyleft license family that must never appear in a first-party
# declaration (issue #63). Written as a character class rather than a literal so
# that this guard file does not itself make the repository-wide
# `git grep -i` copyleft acceptance check come back dirty.
STRONG_COPYLEFT_RE = re.compile(r"(?i)\bA[G]PL\b")

# Dependency lockfiles / manifests legitimately record *upstream* licenses and
# are not this repository's own grant.
_THIRD_PARTY_LICENSE_FILES = {
    "package-lock.json",
    "web/package-lock.json",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-lock.txt",
}

_TEXT_SUFFIXES = {".py", ".toml", ".cfg", ".ini", ".md", ".txt", ".json", ".yaml", ".yml"}


def _tracked_files() -> list[str]:
    """Repo-relative paths of git-tracked files, falling back to a walk.

    Tracked-only matters: sibling agent/git worktrees under ``.worktrees/`` and
    ``.claude/worktrees/`` are untracked checkouts of other branches and are not
    part of this repository's declared grant.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=60,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return [
            p.relative_to(REPO_ROOT).as_posix()
            for p in sorted(REPO_ROOT.rglob("*"))
            if p.is_file()
            and not p.relative_to(REPO_ROOT)
            .as_posix()
            .startswith((".git/", ".worktrees/", ".claude/", "node_modules/", "htmlcov/"))
        ]
    return [p for p in out.decode("utf-8").split("\0") if p]


class TestLicenseContract:
    """One license, one declaration (issue #63).

    ``pyproject.toml`` used to declare a strong-copyleft SPDX expression while
    ``LICENSE``, the README and GitHub's own license detection all resolved to
    ``Apache-2.0``. Packaging metadata that contradicts the license actually
    granted is worse than missing metadata, so the grant is asserted here rather
    than trusted to review.
    """

    CANONICAL_SPDX = "Apache-2.0"

    def test_license_file_is_the_canonical_apache_2_0_text(self):
        text = _read("LICENSE")
        assert "Apache License" in text
        assert "Version 2.0, January 2004" in text
        assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text
        # The canonical text ends in the boilerplate appendix; a truncated or
        # otherwise damaged LICENSE must not pass as a valid grant.
        assert "END OF TERMS AND CONDITIONS" in text
        assert "APPENDIX: How to apply the Apache License to your work." in text
        assert "http://www.apache.org/licenses/LICENSE-2.0" in text

    def test_pyproject_license_is_the_canonical_spdx_expression(self):
        text = _read("pyproject.toml")
        # Accept either the PEP 639 string form or the legacy PEP 621 table
        # form; what matters is the license granted, not its encoding.
        m = re.search(
            r'(?m)^license\s*=\s*(?:\{[^}]*text\s*=\s*)?["\']([^"\']+)["\']',
            text,
        )
        assert m, "pyproject.toml license declaration not found"
        assert m.group(1) == self.CANONICAL_SPDX, (
            f"pyproject.toml declares {m.group(1)!r} but the canonical LICENSE "
            f"grants {self.CANONICAL_SPDX}"
        )

    def test_pyproject_license_files_points_at_the_canonical_license(self):
        text = _read("pyproject.toml")
        m = re.search(r"(?m)^license-files\s*=\s*\[(.*?)\]", text, re.DOTALL)
        assert m, "pyproject.toml license-files not declared"
        assert "LICENSE" in m.group(1)

    def test_readme_states_the_canonical_license(self):
        readme = _read("README.md")
        assert "Apache 2.0" in readme, "README no longer states Apache 2.0"
        assert STRONG_COPYLEFT_RE.search(readme) is None, (
            "README asserts a license that contradicts the canonical LICENSE"
        )

    def test_no_first_party_conflicting_license_declaration(self):
        """No tracked first-party file may declare a strong copyleft license.

        This is the machine-checked form of the `git grep -i` copyleft
        acceptance gate for issue #63: the canonical Apache-2.0 grant must be
        the only license this repository declares.
        """
        offenders = []
        for rel in _tracked_files():
            if rel in _THIRD_PARTY_LICENSE_FILES:
                continue
            path = REPO_ROOT / rel
            if path.suffix not in _TEXT_SUFFIXES:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if STRONG_COPYLEFT_RE.search(content):
                offenders.append(rel)
        assert not offenders, (
            f"first-party files declare a license contradicting the canonical LICENSE: {offenders}"
        )


class TestCurrentTruthUniqueness:
    def test_canonical_current_state_exists(self):
        assert (REPO_ROOT / "docs/reference/current-state.md").exists()

    def test_entry_docs_point_to_current_state(self):
        for rel in ("README.md", "CLAUDE.md", "docs/README.md"):
            assert "reference/current-state.md" in _read(rel), (
                f"{rel} does not point at the canonical current-state entry"
            )

    def test_no_competing_current_truth_doc(self):
        docs_root = REPO_ROOT / "docs"
        names = {
            p.name
            for p in docs_root.rglob("*.md")
            if ("current" in p.name and ("truth" in p.name or "state" in p.name))
            and "reports" not in p.parts
            and "archive" not in p.parts
        }
        assert names == {"current-state.md"}, f"competing current-truth docs: {sorted(names)}"
