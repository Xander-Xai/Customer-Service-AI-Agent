"""统一的代码版本元数据（provenance）—— 供所有正式证据生成器复用。

背景缺陷
--------
``scripts/evaluate_agent.py`` / ``measure_performance.py`` /
``report_distributed_runtime.py`` / ``verify_metrics_exposure.sh`` 过去只记录
``git rev-parse HEAD``。于是在**脏工作区**（有未提交修改）里生成的 artifact
会声称"对应 commit X"，而实际被测代码并不等于 commit X —— 数字无法复现，
却看起来像可复现证据。这是证据链最隐蔽的一种谎言：commit 是真的，代码不是。

本模块把"代码版本"拆成三个必须同时记录的维度：

1. ``commit_sha`` —— HEAD 提交；
2. ``dirty`` —— 工作区是否有未提交修改（tracked diff 或未跟踪源文件）；
3. 脏时的**内容指纹**：``tracked_diff_sha256`` + ``untracked_source_sha256``。

纪律
----
* ``dirty=True`` 只是**提示**，它**不能**替代一个可复现的固定提交。正式对外
  证据必须在代码提交后、干净工作区里重新生成。
* 若生成 artifact 的过程修改了被测源文件，``publication_status`` 必须标为
  ``UNPUBLISHABLE``。
* 不写入任何 token / 密钥 / 敏感业务数据 —— 只记录哈希与版本号。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 参与"未跟踪源文件"指纹的文件扩展名。只哈希源码，避免把临时 JSON / 日志
#: 等无关产物混进指纹（它们不构成"被测代码"）。
_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".sh",
        ".yml",
        ".yaml",
        ".toml",
        ".cfg",
        ".ini",
        ".sql",
        ".html",
        ".css",
        ".jsonl",
    }
)

REPRODUCIBLE = "REPRODUCIBLE"
DIRTY_WORKTREE = "DIRTY_WORKTREE_NOT_REPRODUCIBLE"
UNPUBLISHABLE = "UNPUBLISHABLE_GENERATOR_MUTATED_SOURCES"


def _run_git(repo_root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout
    except Exception:  # pragma: no cover - git 缺失时退化为无版本信息
        return 1, ""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tool_versions() -> dict[str, str]:
    versions: dict[str, str] = {"python": sys.version.split()[0]}
    try:
        from importlib.metadata import PackageNotFoundError, version

        for pkg in ("langgraph", "langchain-core", "fastapi", "qdrant-client", "celery"):
            try:
                versions[pkg] = version(pkg)
            except PackageNotFoundError:
                continue
    except Exception:  # pragma: no cover
        pass
    return versions


@dataclass
class CodeProvenance:
    """一次证据生成时的代码版本快照。"""

    commit_sha: str | None
    dirty: bool
    tracked_diff_sha256: str | None
    untracked_source_sha256: str | None
    tool_versions: dict[str, str]
    generated_at: str
    dataset_sha256: str | None = None
    publication_status: str = REPRODUCIBLE
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "dirty": self.dirty,
            "tracked_diff_sha256": self.tracked_diff_sha256,
            "untracked_source_sha256": self.untracked_source_sha256,
            "tool_versions": dict(self.tool_versions),
            "generated_at": self.generated_at,
            "dataset_sha256": self.dataset_sha256,
            "publication_status": self.publication_status,
            "notes": list(self.notes),
        }


def collect_code_provenance(
    repo_root: Path | str,
    *,
    dataset_path: Path | str | None = None,
    generator_mutated_sources: bool = False,
) -> CodeProvenance:
    """采集代码版本元数据。**不**产生任何副作用（除只读 git 查询）。"""
    root = Path(repo_root)

    rc, head = _run_git(root, "rev-parse", "HEAD")
    commit_sha = head.strip() if rc == 0 and head.strip() else None

    # tracked diff：HEAD 与工作区（含已暂存）的差异。--binary 保证二进制也计入。
    _, tracked = _run_git(root, "diff", "HEAD", "--binary")
    rc_status, status = _run_git(root, "status", "--porcelain")
    tracked_diff_sha256 = _sha256_hex(tracked.encode("utf-8")) if tracked else None

    # 未跟踪源文件：逐文件哈希后拼成稳定指纹（排序保证可复现）。
    _, untracked_list = _run_git(root, "ls-files", "--others", "--exclude-standard")
    untracked_files = [
        line.strip()
        for line in untracked_list.splitlines()
        if line.strip() and Path(line.strip()).suffix in _SOURCE_SUFFIXES
    ]
    untracked_digest = hashlib.sha256()
    for rel in sorted(untracked_files):
        path = root / rel
        try:
            content = path.read_bytes()
        except OSError:
            continue
        untracked_digest.update(rel.encode("utf-8"))
        untracked_digest.update(b"\0")
        untracked_digest.update(content)
        untracked_digest.update(b"\0")
    untracked_source_sha256 = untracked_digest.hexdigest() if untracked_files else None

    dirty = bool(status.strip()) if rc_status == 0 else False

    dataset_sha256 = None
    if dataset_path is not None:
        try:
            dataset_sha256 = _sha256_hex(Path(dataset_path).read_bytes())
        except OSError:
            dataset_sha256 = None

    notes: list[str] = []
    if dirty:
        notes.append(
            "dirty worktree: commit_sha alone does NOT identify the tested code. "
            "Regenerate this artifact from a committed, clean worktree before using it "
            "as public evidence."
        )
    if generator_mutated_sources:
        notes.append("the generator mutated tested sources; this artifact is not publishable.")

    if generator_mutated_sources:
        publication_status = UNPUBLISHABLE
    elif dirty:
        publication_status = DIRTY_WORKTREE
    else:
        publication_status = REPRODUCIBLE

    return CodeProvenance(
        commit_sha=commit_sha,
        dirty=dirty,
        tracked_diff_sha256=tracked_diff_sha256,
        untracked_source_sha256=untracked_source_sha256,
        tool_versions=_tool_versions(),
        generated_at=datetime.now(timezone.utc).isoformat(),
        dataset_sha256=dataset_sha256,
        publication_status=publication_status,
        notes=notes,
    )


__all__ = [
    "DIRTY_WORKTREE",
    "REPRODUCIBLE",
    "UNPUBLISHABLE",
    "CodeProvenance",
    "collect_code_provenance",
]
