"""监控 Token 目录的 Git ignore 契约。

历史缺陷：``.gitignore`` 里的 ``secrets/`` 规则会把整个
``deploy/monitoring/secrets/`` 目录忽略掉（git 语义：父目录被忽略时无法单独
re-include 子文件），于是新 clone 后该目录不存在，Prometheus 的 bind mount
失效、容器起不来。本测试锁定三条不变量：

1. 目录占位文件（``.gitkeep`` / ``README.md``）**不被忽略**，可被 Git 跟踪；
2. 真实 token 文件**永远被忽略**；
3. token 生成器与 Prometheus 抓取配置指向**同一个**文件路径。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRETS_DIR = REPO_ROOT / "deploy" / "monitoring" / "secrets"
TOKEN_FILE = "deploy/monitoring/secrets/monitoring_admin_token"
KEEP_FILES = (
    "deploy/monitoring/secrets/.gitkeep",
    "deploy/monitoring/secrets/README.md",
)

pytestmark = pytest.mark.unit


def _is_ignored(rel_path: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", rel_path],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


class TestMonitoringSecretsIgnoreContract:
    def test_token_file_is_always_ignored(self):
        assert _is_ignored(TOKEN_FILE), "真实 token 文件必须被 .gitignore 忽略"

    def test_placeholder_files_are_not_ignored(self):
        for rel in KEEP_FILES:
            assert not _is_ignored(rel), (
                f"{rel} 不应被忽略 —— 否则新 clone 后目录不存在，Prometheus " f"bind mount 失效"
            )

    def test_token_file_is_not_tracked(self):
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", TOKEN_FILE],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, "真实 token 文件绝不能进入版本库"

    def test_generator_and_prometheus_agree_on_path(self):
        import re

        prometheus = (REPO_ROOT / "monitoring" / "prometheus.yml").read_text(encoding="utf-8")
        assert "/etc/prometheus/secrets/monitoring_admin_token" in prometheus

        generator = (REPO_ROOT / "scripts" / "generate_monitoring_token.py").read_text(
            encoding="utf-8"
        )
        # 生成器写到宿主机 deploy/monitoring/secrets/monitoring_admin_token，
        # 而 compose 把它挂到 /etc/prometheus/secrets/ —— 两者必须是同一个文件。
        assert "deploy" in generator and "monitoring" in generator and "secrets" in generator
        compose = (REPO_ROOT / "deploy" / "compose" / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        assert re.search(r"\.\./monitoring/secrets:/etc/prometheus/secrets", compose)


class TestTokenGeneratorFailClosed:
    def _run(self, env: dict[str, str]) -> subprocess.CompletedProcess:
        import os

        base = dict(os.environ)
        base.pop("MONITORING_ADMIN_TOKEN", None)
        base.update(env)
        return subprocess.run(
            ["python3", str(REPO_ROOT / "scripts" / "generate_monitoring_token.py")],
            capture_output=True,
            text=True,
            env=base,
            cwd=str(REPO_ROOT),
        )

    def test_short_token_fails_closed(self):
        target = SECRETS_DIR / "monitoring_admin_token"
        before = target.read_bytes() if target.exists() else None
        proc = self._run({"MONITORING_ADMIN_TOKEN": "short"})
        assert proc.returncode == 1
        after = target.read_bytes() if target.exists() else None
        assert after == before, "fail-closed 时不得改动凭据文件"

    def test_placeholder_token_fails_closed(self):
        proc = self._run({"MONITORING_ADMIN_TOKEN": "change-me"})
        assert proc.returncode == 1
