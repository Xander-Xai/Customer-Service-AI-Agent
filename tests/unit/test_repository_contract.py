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
            "make lock overwrites requirements-lock.txt, destroying its "
            "non-authoritative header"
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
            "CODEX_PROJECT_REMEDIATION_SPEC.md reclaimed its superseded "
            "'唯一执行规格' authority"
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
