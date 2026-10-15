"""Runtime Chaos 证据的源码溯源契约（``make runtime-chaos`` → ``chaos-*.json``）。

历史缺口：``scripts/test_worker_crash_recovery.py::main`` 直接 ``json.dumps`` 业务
结果，因此混沌报告既没有 commit，也没有"工作区是否干净"，更没有脏内容指纹。
它声称的是一次真实基础设施上的崩溃恢复结论，却无法回答**这段结论测的是哪份
代码**——这正是 ``core/code_provenance.py`` 存在的理由。

本模块钉住四件事：

1. 报告顶层必须有 ``code_provenance``，且内容与公共收集器**逐字段一致**
   （不重命名、不平铺、不自造语义）；
2. PASS 与 FAIL **都**保留原结论与退出码 —— 溯源采集失败不许把 FAIL 抬成 PASS；
3. git 不可用 / 无 HEAD 时**如实**标为 unavailable，绝不编造 commit；
4. 旧版（无 ``code_provenance`` 的）``chaos-*.json`` 仍是合法历史状态，下游
   工具照常消费，且不会被原地改写。

全部用例都不需要 PostgreSQL / Redis / Celery：``run()`` 被替换为桩，因此这里
验证的是**报告装配层**，不是崩溃恢复机制本身（后者由
``tests/integration/runtime`` 与 ``make runtime-chaos`` 在真实基础设施上验证）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.code_provenance import (
    DIRTY_WORKTREE,
    REPRODUCIBLE,
    UNAVAILABLE,
    CodeProvenance,
    collect_code_provenance,
)
from scripts import report_distributed_runtime as reporter
from scripts import test_worker_crash_recovery as chaos

pytestmark = pytest.mark.unit


PASS_REPORT = {
    "result": "PASS",
    "run_id": "chaos-unit0001",
    "thread_id": "T-chaos-unit0001",
    "queue": "agent_runs_test_unit0001",
    "steps": [
        {"step": 1, "action": "create_run", "run_id": "chaos-unit0001", "ok": True},
        {"step": 5, "action": "sigkill_worker_group", "ok": True},
    ],
}

FAIL_REPORT = {
    "result": "FAIL",
    "run_id": "chaos-unit0002",
    "thread_id": "T-chaos-unit0002",
    "error": "ChaosFailure: 副作用被重复执行：idem:counter=2（必须 == 1）",
    "steps": [{"step": 3, "action": "side_effect_applied", "idem:counter": 1, "ok": True}],
}

PROVENANCE_KEYS = set(
    CodeProvenance(
        commit_sha="0" * 40,
        dirty=False,
        tracked_diff_sha256=None,
        untracked_source_sha256=None,
        tool_versions={},
        generated_at="1970-01-01T00:00:00+00:00",
    ).to_dict()
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "agent.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "agent.py")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run_main(monkeypatch, report: dict, out_path: Path, capsys) -> tuple[str, int]:
    """执行脚本 ``main()``，返回 (stdout, 退出码)。``run()`` 被替换为桩。"""
    monkeypatch.setattr(chaos, "run", lambda: json.loads(json.dumps(report)))
    monkeypatch.setattr(sys, "argv", ["test_worker_crash_recovery.py", "--output", str(out_path)])
    exit_code = chaos.main()
    return capsys.readouterr().out, exit_code


def _head_sha_or_none() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(chaos.REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - 无 git 二进制
        return None
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


# --------------------------------------------------------------------------- #
# A. 正常输出
# --------------------------------------------------------------------------- #


def test_report_carries_real_code_provenance(tmp_path: Path, monkeypatch, capsys):
    """A1：顶层有 code_provenance，且 commit 与真实工作区一致。"""
    out_path = tmp_path / "chaos.json"
    _run_main(monkeypatch, PASS_REPORT, out_path, capsys)

    report = json.loads(out_path.read_text(encoding="utf-8"))
    prov = report["code_provenance"]
    assert set(prov) == PROVENANCE_KEYS
    assert prov["commit_sha"] == _head_sha_or_none()
    # 不得遗漏真实 Git 状态：clean/dirty 与 public collector 同口径。
    assert prov["dirty"] == collect_code_provenance(chaos.REPO_ROOT).dirty
    assert prov["publication_status"] in {
        REPRODUCIBLE,
        DIRTY_WORKTREE,
        UNAVAILABLE,
        "UNPUBLISHABLE_GENERATOR_MUTATED_SOURCES",
    }


def test_provenance_is_verbatim_collector_output(tmp_path: Path, monkeypatch, capsys):
    """A2：内容与公共收集器逐字段一致 —— 不重命名、不平铺、不自造语义。"""
    fixed = CodeProvenance(
        commit_sha="a" * 40,
        dirty=True,
        tracked_diff_sha256="d" * 64,
        untracked_source_sha256="u" * 64,
        tool_versions={"python": "3.10.12", "celery": "5.6.3"},
        generated_at="2026-01-01T00:00:00+00:00",
        publication_status=DIRTY_WORKTREE,
        notes=["dirty worktree: ..."],
    )
    monkeypatch.setattr("core.code_provenance.collect_code_provenance", lambda root: fixed)
    out_path = tmp_path / "chaos.json"
    _run_main(monkeypatch, PASS_REPORT, out_path, capsys)

    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["code_provenance"] == fixed.to_dict()


def test_stdout_and_written_file_are_identical(tmp_path: Path, monkeypatch, capsys):
    """A3：stdout 与 --output 是同一份序列化结果（逐字节前缀一致）。"""
    out_path = tmp_path / "nested" / "chaos.json"
    stdout, exit_code = _run_main(monkeypatch, PASS_REPORT, out_path, capsys)

    written = out_path.read_text(encoding="utf-8")
    assert exit_code == 0
    assert stdout.startswith(written)
    assert json.loads(written) == json.loads(stdout[: len(written)])
    assert "[PASS] runtime-chaos" in stdout


def test_provenance_block_carries_no_credentials(tmp_path: Path, monkeypatch, capsys):
    """A4：溯源块只有已知字段，不夹带 DSN / Token / 口令。"""
    out_path = tmp_path / "chaos.json"
    _run_main(monkeypatch, PASS_REPORT, out_path, capsys)

    report = json.loads(out_path.read_text(encoding="utf-8"))
    prov = report["code_provenance"]
    assert set(prov) == PROVENANCE_KEYS
    blob = json.dumps(prov, ensure_ascii=False).lower()
    for needle in ("password", "token", "secret", "api_key", "postgres://", "redis://"):
        assert needle not in blob
    # 业务侧的连接信息由 Makefile / 运行环境提供，provenance 不复制它。
    if chaos.DB_URL:
        assert chaos.DB_URL not in json.dumps(prov)
    if chaos.REDIS_URL:
        assert chaos.REDIS_URL not in json.dumps(prov)


# --------------------------------------------------------------------------- #
# B. 脏工作区
# --------------------------------------------------------------------------- #


def test_dirty_worktree_is_reported_as_not_reproducible(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys
):
    """B1：脏工作区 → dirty=True + 指纹存在 + 绝不声明 CLEAN / 可发布。"""
    (git_repo / "agent.py").write_text("x = 2  # 未提交\n", encoding="utf-8")
    monkeypatch.setattr(chaos, "REPO_ROOT", git_repo)

    out_path = tmp_path / "chaos.json"
    _run_main(monkeypatch, PASS_REPORT, out_path, capsys)

    prov = json.loads(out_path.read_text(encoding="utf-8"))["code_provenance"]
    assert prov["dirty"] is True
    assert prov["tracked_diff_sha256"]
    assert prov["publication_status"] == DIRTY_WORKTREE
    assert prov["publication_status"] != REPRODUCIBLE
    assert prov["notes"], "脏工作区必须留下解释性 note"


def test_untracked_source_is_fingerprinted_not_ignored(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys
):
    """B2：未跟踪源文件进入指纹；未跟踪 JSON 产物不把证据拖成 dirty。"""
    monkeypatch.setattr(chaos, "REPO_ROOT", git_repo)
    out_path = tmp_path / "chaos.json"
    _run_main(monkeypatch, PASS_REPORT, out_path, capsys)
    clean = json.loads(out_path.read_text(encoding="utf-8"))["code_provenance"]
    assert clean["untracked_source_sha256"] is None
    assert clean["dirty"] is False
    assert clean["publication_status"] == REPRODUCIBLE

    (git_repo / "extra_tool.py").write_text("y = 1\n", encoding="utf-8")
    (git_repo / "report.json").write_text("{}\n", encoding="utf-8")
    _run_main(monkeypatch, PASS_REPORT, out_path, capsys)
    dirty = json.loads(out_path.read_text(encoding="utf-8"))["code_provenance"]
    assert dirty["dirty"] is True
    assert dirty["untracked_source_sha256"]
    assert dirty["publication_status"] == DIRTY_WORKTREE


# --------------------------------------------------------------------------- #
# C. 无法识别 Git 版本
# --------------------------------------------------------------------------- #


def test_unavailable_git_identity_is_not_faked(tmp_path: Path, monkeypatch, capsys):
    """C1：非 git 目录 → commit_sha 为 None 且显式 unavailable。"""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    monkeypatch.setattr(chaos, "REPO_ROOT", not_a_repo)

    out_path = tmp_path / "chaos.json"
    stdout, exit_code = _run_main(monkeypatch, PASS_REPORT, out_path, capsys)

    prov = json.loads(out_path.read_text(encoding="utf-8"))["code_provenance"]
    assert prov["commit_sha"] is None
    assert prov["publication_status"] == UNAVAILABLE
    assert prov["publication_status"] != REPRODUCIBLE
    assert any("unavailable" in note for note in prov["notes"])
    # 业务判定不受溯源可用性影响。
    assert exit_code == 0
    assert "[PASS] runtime-chaos" in stdout


def test_collector_failure_degrades_without_touching_verdict(tmp_path: Path, monkeypatch, capsys):
    """C2：采集器本身抛异常 → UNAVAILABLE 块，混沌判定照旧。"""

    def boom(root):
        raise RuntimeError("git exploded")

    monkeypatch.setattr("core.code_provenance.collect_code_provenance", boom)
    out_path = tmp_path / "chaos.json"
    _, exit_code = _run_main(monkeypatch, PASS_REPORT, out_path, capsys)

    prov = json.loads(out_path.read_text(encoding="utf-8"))["code_provenance"]
    assert prov["publication_status"] == chaos.UNAVAILABLE_STATUS
    assert prov["commit_sha"] is None
    assert any("git exploded" in note for note in prov["notes"])
    assert exit_code == 0


def test_unavailable_status_matches_shared_constant():
    """脚本内的字面量必须与公共收集器常量同值（否则同一状态两种拼写）。"""
    assert chaos.UNAVAILABLE_STATUS == UNAVAILABLE


# --------------------------------------------------------------------------- #
# D. 故障路径
# --------------------------------------------------------------------------- #


def test_failure_report_keeps_error_and_exit_code(tmp_path: Path, monkeypatch, capsys):
    """D1：混沌 FAIL 时原因、步骤、退出码与修复前完全一致。"""
    out_path = tmp_path / "chaos.json"
    stdout, exit_code = _run_main(monkeypatch, FAIL_REPORT, out_path, capsys)

    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["result"] == "FAIL"
    assert report["error"] == FAIL_REPORT["error"]
    assert report["steps"] == FAIL_REPORT["steps"]
    assert exit_code == 1, "既有退出码语义不得改变"
    assert "[FAIL] runtime-chaos" in stdout
    # 失败报告同样带溯源：失败证据同样要能指出测的是哪份代码。
    assert report["code_provenance"]["commit_sha"] == _head_sha_or_none()


def test_provenance_never_promotes_failure_to_pass(tmp_path: Path, monkeypatch, capsys):
    """D2：溯源采集失败也不许把 FAIL 抬成 PASS。"""
    monkeypatch.setattr(
        "core.code_provenance.collect_code_provenance",
        lambda root: (_ for _ in ()).throw(RuntimeError("no git here")),
    )
    out_path = tmp_path / "chaos.json"
    _, exit_code = _run_main(monkeypatch, FAIL_REPORT, out_path, capsys)

    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["result"] == "FAIL"
    assert report["code_provenance"]["publication_status"] == chaos.UNAVAILABLE_STATUS
    assert exit_code == 1


def test_provenance_is_not_collected_inside_the_run_body(monkeypatch):
    """D3：溯源不得插进 run() 的业务路径 —— 故障注入逻辑保持原样。"""
    source = Path(chaos.__file__).read_text(encoding="utf-8")
    run_body = source.split("\ndef run(", 1)[1].split("\ndef _row(", 1)[0]
    assert "code_provenance" not in run_body
    assert "collect_code_provenance" not in run_body


# --------------------------------------------------------------------------- #
# E. 历史兼容性
# --------------------------------------------------------------------------- #


def _legacy_chaos_artifact(path: Path) -> None:
    """2026-10 之前的历史形态：合法、无 schema、无 code_provenance。"""
    path.write_text(json.dumps(PASS_REPORT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _use_repo_root(monkeypatch, tmp_path: Path) -> None:
    """汇总工具按 REPO_ROOT 计算相对路径；把它的根换到 tmp 目录，
    测试就无需往仓库树里写文件。"""
    monkeypatch.setattr(reporter, "REPO_ROOT", tmp_path)


def test_legacy_artifact_without_provenance_is_still_consumable(tmp_path: Path, monkeypatch):
    """E1：旧 chaos-*.json 没有该字段，下游汇总工具照常读取。"""
    legacy = tmp_path / "chaos-20261006T081808Z.json"
    _legacy_chaos_artifact(legacy)
    verify = tmp_path / "report.json"
    verify.write_text(
        json.dumps({"schema_version": "distributed-runtime-evidence/v2", "overall_status": "PASS"})
        + "\n",
        encoding="utf-8",
    )
    before = legacy.read_bytes()

    _use_repo_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        reporter,
        "_latest",
        lambda pattern: legacy if pattern.startswith("artifacts/runtime") else verify,
    )
    out_path = tmp_path / "summary.json"
    assert reporter.main(["--out", str(out_path)]) == 0

    summary = json.loads(out_path.read_text(encoding="utf-8"))
    assert summary["overall_status"] == "PASS"
    assert summary["side_effect_accounting"]["canonical_metric"] == "real_side_effects"
    assert legacy.read_bytes() == before, "历史产物不得被原地补写或重生成"


def test_new_artifact_with_provenance_is_consumable(tmp_path: Path, monkeypatch):
    """E2：新增顶层字段不影响同一读取路径（无严格 schema 拒绝）。"""
    fresh = tmp_path / "chaos-20990101T000000Z.json"
    fresh.write_text(
        json.dumps({**PASS_REPORT, "code_provenance": {"commit_sha": "b" * 40}}, indent=2) + "\n",
        encoding="utf-8",
    )
    verify = tmp_path / "report.json"
    verify.write_text(json.dumps({"overall_status": "PASS"}) + "\n", encoding="utf-8")
    _use_repo_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        reporter,
        "_latest",
        lambda pattern: fresh if pattern.startswith("artifacts/runtime") else verify,
    )

    assert reporter.main(["--out", str(tmp_path / "summary.json")]) == 0


#: provenance 修复**之前**提交的 8 份 chaos 证据。断言只针对这一组已知历史文件：
#: 将来在干净工作区里新生成并正式入库的 chaos artifact **应该**带
#: ``code_provenance``（见 artifacts/README.md 的可复现纪律），不能被本用例误伤。
LEGACY_CHAOS_ARTIFACTS = (
    "artifacts/runtime/chaos-20261002T053011Z.json",
    "artifacts/runtime/chaos-20261002T082222Z.json",
    "artifacts/runtime/chaos-20261002T094500Z.json",
    "artifacts/runtime/chaos-20261002T100500Z.json",
    "artifacts/runtime/chaos-20261002T113006Z.json",
    "artifacts/runtime/chaos-20261002T141717Z.json",
    "artifacts/runtime/chaos-20261002T143800Z.json",
    "artifacts/runtime/chaos-20261006T081808Z.json",
)


def _git_out(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(chaos.REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def test_legacy_chaos_artifacts_are_not_rewritten():
    """E3：8 份历史产物既没被删除/改名，也没被就地补写溯源字段。

    断言**只**针对 ``LEGACY_CHAOS_ARTIFACTS``。将来在干净工作区里新生成并正式
    入库的 chaos artifact 应当带 ``code_provenance``（artifacts/README.md 的可复现
    纪律），因此它必须落在本用例的遍历范围之外 —— 否则一个正确的入库会被误判为
    "篡改历史"。
    """
    tracked = set(_git_out("ls-files", "artifacts/runtime").split())
    for rel in LEGACY_CHAOS_ARTIFACTS:
        assert rel in tracked, f"历史产物 {rel} 不应被删除或改名"
        content = json.loads((chaos.REPO_ROOT / rel).read_text(encoding="utf-8"))
        assert "code_provenance" not in content, f"历史产物 {rel} 不得被补写溯源字段"

    modified = _git_out("diff", "--name-only", "HEAD", "--", *LEGACY_CHAOS_ARTIFACTS)
    assert not modified.strip(), f"历史产物被就地改写: {modified.strip()}"

    fresh_name = "artifacts/runtime/chaos-20990101T000000Z.json"
    assert fresh_name not in LEGACY_CHAOS_ARTIFACTS
