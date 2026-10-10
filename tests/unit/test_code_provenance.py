"""代码溯源契约：脏工作区不得冒充可复现证据。

历史缺陷：生成器只记录 ``git rev-parse HEAD``，于是"在脏工作区里跑出来的
数字"会声称对应某个 commit，而实际代码 ≠ 该 commit。本测试证明不同工作区
内容会得到不同的指纹，脏工作区不会被标成 REPRODUCIBLE。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.code_provenance import (
    DIRTY_WORKTREE,
    REPRODUCIBLE,
    UNPUBLISHABLE,
    collect_code_provenance,
)

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_clean_worktree_is_reproducible(git_repo: Path):
    prov = collect_code_provenance(git_repo)
    assert prov.commit_sha
    assert prov.dirty is False
    assert prov.publication_status == REPRODUCIBLE
    assert prov.tracked_diff_sha256 is None


def test_dirty_worktree_is_not_reproducible(git_repo: Path):
    (git_repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    prov = collect_code_provenance(git_repo)
    assert prov.dirty is True
    assert prov.publication_status == DIRTY_WORKTREE
    assert prov.tracked_diff_sha256


def test_different_diffs_produce_different_fingerprints(git_repo: Path):
    (git_repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    first = collect_code_provenance(git_repo)
    (git_repo / "app.py").write_text("x = 3\n", encoding="utf-8")
    second = collect_code_provenance(git_repo)
    # 同一 commit、不同工作区内容 => 指纹必须不同，否则会错误地生成
    # 同一份"可复现证据"声明。
    assert first.commit_sha == second.commit_sha
    assert first.tracked_diff_sha256 != second.tracked_diff_sha256


def test_untracked_source_changes_are_captured(git_repo: Path):
    clean = collect_code_provenance(git_repo)
    assert clean.untracked_source_sha256 is None
    (git_repo / "new_module.py").write_text("y = 1\n", encoding="utf-8")
    prov = collect_code_provenance(git_repo)
    assert prov.dirty is True
    assert prov.untracked_source_sha256 is not None
    (git_repo / "new_module.py").write_text("y = 2\n", encoding="utf-8")
    prov2 = collect_code_provenance(git_repo)
    assert prov.untracked_source_sha256 != prov2.untracked_source_sha256


def test_untracked_non_source_artifact_does_not_mark_dirty(git_repo: Path):
    # 运行产物（JSON）不影响被测代码，不应让 provenance 变"脏"。
    (git_repo / "report.json").write_text("{}\n", encoding="utf-8")
    prov = collect_code_provenance(git_repo)
    assert prov.dirty is False
    assert prov.publication_status == REPRODUCIBLE


def test_generator_mutating_sources_is_unpublishable(git_repo: Path):
    prov = collect_code_provenance(git_repo, generator_mutated_sources=True)
    assert prov.publication_status == UNPUBLISHABLE


def test_no_secrets_in_provenance(git_repo: Path):
    payload = collect_code_provenance(git_repo).to_dict()
    text = str(payload).lower()
    assert "token" not in text
    assert "secret" not in text
    assert "password" not in text
