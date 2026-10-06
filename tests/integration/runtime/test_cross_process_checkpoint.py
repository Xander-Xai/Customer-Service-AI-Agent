"""Gate 1：PostgreSQL checkpoint 的**跨进程**持久化与恢复。

验收要求（缺一即 FAIL）::

    Process A -> checkpoint persisted -> Process A terminated
              -> Process B (全新进程) -> checkpoint 成功读回

与已有 ``tests/integration/test_checkpoint_postgres.py`` 的区别：那一支在**同一
Python 进程**里建两个 saver，重启只是"关掉连接池再重建"，不是真的换进程。
本文件用 ``subprocess`` 启动两个独立 OS 进程，读写都发生在真实 PostgreSQL 上，
不 mock checkpointer、不依赖 MemorySaver。

另外验证：同库不同 thread 之间状态严格隔离。

运行::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    pytest tests/integration/runtime/test_cross_process_checkpoint.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL 未设置；需要真实 PostgreSQL"
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip(), reason=INFRA_REASON),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "tests" / "integration" / "runtime" / "checkpoint_subprocess.py"


def _run(mode: str, thread_id: str, db_url: str, timeout: int = 120) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), mode, thread_id, db_url],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            **os.environ,
            "DEV_MODE": "true",
            "LANGGRAPH_CHECKPOINT_BACKEND": "postgres",
            "LANGGRAPH_CHECKPOINT_DATABASE_URL": db_url,
        },
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"{mode} 子进程失败 rc={proc.returncode}\n"
            f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
        )
    line = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")][-1]
    return json.loads(line)


@pytest.mark.timeout(300)
def test_gate1_checkpoint_survives_process_restart(pg_url: str):
    """进程 A 写入 -> 进程 A 结束 -> 全新进程 B 读回同一 thread 的 checkpoint。"""
    thread_id = f"gate1-{uuid.uuid4().hex[:10]}"

    written = _run("write", thread_id, pg_url)
    assert written["checkpoint_backend"] == "postgres", written
    assert written["result"]["n"] == 20, f"图未跑完（1 -> +1=2 -> *10=20）: {written}"
    assert written["result"]["trail"] == "AB"

    # 进程 A 已随 subprocess.run 结束。这里是**另一个全新进程**。
    read_back = _run("read", thread_id, pg_url)

    assert read_back["checkpoint_backend"] == "postgres"
    assert read_back["pid"] != written["pid"], "必须是不同进程"
    assert read_back["values"].get("n") == 20, f"新进程未读回 checkpoint: {read_back['values']}"
    assert read_back["values"].get("trail") == "AB"

    # 读回后能在同一 lineage 继续执行（证明 checkpoint 状态真实可用）
    assert read_back["continued"].get("n") == 210, read_back["continued"]


@pytest.mark.timeout(300)
def test_gate1_threads_are_isolated(pg_url: str):
    """同库不同 thread 的状态必须隔离：读 A 不会拿到 B 的值。"""
    thread_a = f"gate1-iso-a-{uuid.uuid4().hex[:8]}"
    thread_b = f"gate1-iso-b-{uuid.uuid4().hex[:8]}"

    a = _run("write", thread_a, pg_url)
    assert a["result"]["n"] == 20

    # B 是全新 thread：不得读到 A 的 checkpoint
    b = _run("read", thread_b, pg_url)
    assert not b["values"], f"新 thread 不应读到任何值: {b['values']}"

    # A 仍然完好
    a_again = _run("read", thread_a, pg_url)
    assert a_again["values"].get("n") == 20


@pytest.mark.timeout(180)
def test_gate1_memory_backend_is_not_used_in_production_config(pg_url: str):
    """生产（非 dev）必须 fail-closed 拒绝 memory checkpointer。"""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from core.config import validate_checkpoint_settings as v;"
                "print(v('memory', dev_mode=False))"
            ),
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "DEV_MODE": "false"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "LANGGRAPH_CHECKPOINT_BACKEND=postgres" in proc.stdout, proc.stdout
