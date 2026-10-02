"""Fresh-DB Alembic migration gate（Gate: empty PostgreSQL → upgrade head → schema）。

在**空白** PostgreSQL database 上运行完整 migration chain，验证：
  base → 001 → 002 → 003 → 8ea0ec90ba74 → 004_distributed_agent_runtime

不使用 `alembic stamp`，不使用 `Base.metadata.create_all()`。

默认跳过。运行：

    TEST_FRESH_DB_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/postgres \
        pytest tests/integration/test_fresh_db_migration.py -q

等价脚本：`python -m tests.integration.fresh_db_migrate`（见模块 docstring）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ADMIN_URL = os.getenv("TEST_FRESH_DB_ADMIN_URL", "").strip()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not ADMIN_URL,
        reason="TEST_FRESH_DB_ADMIN_URL 未设置；需要真实 PostgreSQL 才能运行",
    ),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.timeout(180)
def test_fresh_database_upgrade_head_creates_full_schema():
    env = dict(os.environ)
    env["FRESH_DB_ADMIN_URL"] = ADMIN_URL
    env.pop("TEST_FRESH_DB_ADMIN_URL", None)
    env.pop("PYTEST_CURRENT_TEST", None)

    proc = subprocess.run(
        [sys.executable, "-m", "tests.integration.fresh_db_migrate"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=150,
    )
    assert proc.returncode == 0, f"fresh migration failed:\n{proc.stdout}\n{proc.stderr}"

    # runner 的最后一行是 JSON summary
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["ok"] is True, summary
    assert summary["alembic_head"] == "004_distributed_agent_runtime"
    assert summary["missing_core"] == []
    assert summary["missing_runtime"] == []
