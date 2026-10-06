"""``AGENT_EXECUTION_MODE`` 契约（Gate: 执行模式真实性）。

防的是两类回归：

1. **文档漂移**：把 ``/api/chat`` 说成"也经过 Worker"。实际 ``/api/chat`` /
   ``/api/chat/stream`` / ``/api/chat/multimodal`` 是**兼容性 inline 路径**，始终在
   API 进程内执行 LangGraph（``api/app.py::_run_graph``）；只有 ``POST /api/runs``
   受该开关控制。
2. **双旋钮打架**：``AGENT_EXECUTION_MODE``（canonical）与历史变量
   ``AGENT_RUN_DISPATCH`` 同时存在。必须保证 canonical 优先、非法值 fail-fast、
   生产不允许 ``inline``。
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve(env: dict[str, str]) -> tuple[str, str]:
    """在干净子进程里解析配置，返回最后一行输出；失败时返回错误尾行。

    必须中和 dotenv：``core/config.py`` 在 import 期执行 ``load_dotenv(override=True)``，
    否则仓库 ``.env`` 会覆盖本用例显式传入的变量，测到的就不是"仓库 .env 的值"。
    """
    full_env = {**os.environ, **env}
    probe = (
        "import dotenv;"
        "dotenv.load_dotenv = lambda *a, **k: False;"
        "from core.config import AGENT_EXECUTION_MODE as M, AGENT_RUN_DISPATCH as D;"
        "print(f'mode={M} dispatch={D}')"
    )
    proc = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", probe],
        cwd=REPO_ROOT,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = (proc.stdout.strip() or proc.stderr.strip()).splitlines()
    return lines[-1] if lines else ""


@pytest.mark.unit
def test_execution_mode_values_are_valid():
    from core.config import AGENT_EXECUTION_MODE, AGENT_RUN_DISPATCH

    assert AGENT_EXECUTION_MODE in ("inline", "queued")
    assert AGENT_RUN_DISPATCH in ("celery", "inline")
    assert (AGENT_EXECUTION_MODE == "queued") == (
        AGENT_RUN_DISPATCH == "celery"
    ), "canonical 模式与实际派发方式必须一致"


@pytest.mark.unit
def test_inline_compat_endpoints_are_documented_as_inline():
    """/api/chat 系列是兼容性 inline 路径，不受该开关控制。"""
    from core.config import AGENT_INLINE_COMPAT_ENDPOINTS, AGENT_QUEUED_RUN_ENDPOINTS

    assert "/api/chat" in AGENT_INLINE_COMPAT_ENDPOINTS
    assert "/api/chat/stream" in AGENT_INLINE_COMPAT_ENDPOINTS
    assert "/api/runs" in AGENT_QUEUED_RUN_ENDPOINTS


@pytest.mark.unit
def test_fast_path_never_goes_through_worker():
    """源码级锁定：``_run_graph`` 直接 ``ainvoke`` 图，不经过 dispatch/queue。"""
    import inspect

    from api.app import _run_graph

    src = inspect.getsource(_run_graph)
    assert "ainvoke" in src, "快路径应直接执行图"
    for forbidden in ("dispatch_run", "apply_async", "execute_run", "celery"):
        assert forbidden not in src, (
            f"快路径 /api/chat 不应触达 {forbidden}：它是兼容性 inline 路径，" "不能宣称经过 Worker"
        )


@pytest.mark.unit
def test_queued_mode_maps_to_celery_dispatch():
    out = _resolve({"AGENT_EXECUTION_MODE": "queued", "AGENT_RUN_DISPATCH": ""})
    assert "mode=queued" in out and "dispatch=celery" in out, out


@pytest.mark.unit
def test_inline_mode_maps_to_inline_dispatch():
    out = _resolve({"AGENT_EXECUTION_MODE": "inline", "AGENT_RUN_DISPATCH": ""})
    assert "mode=inline" in out and "dispatch=inline" in out, out


@pytest.mark.unit
def test_legacy_dispatch_var_still_works():
    """向后兼容：只设 AGENT_RUN_DISPATCH 也要能解析出 mode。"""
    out = _resolve({"AGENT_EXECUTION_MODE": "", "AGENT_RUN_DISPATCH": "celery"})
    assert "mode=queued" in out and "dispatch=celery" in out, out


@pytest.mark.unit
def test_canonical_mode_wins_over_legacy_var():
    """canonical 旋钮优先：不应因为 .env 里留着旧变量就拒绝启动。"""
    out = _resolve({"AGENT_EXECUTION_MODE": "queued", "AGENT_RUN_DISPATCH": "inline"})
    assert (
        "mode=queued" in out and "dispatch=celery" in out
    ), f"AGENT_EXECUTION_MODE 应覆盖 AGENT_RUN_DISPATCH: {out}"


@pytest.mark.unit
def test_invalid_mode_fails_fast():
    out = _resolve({"AGENT_EXECUTION_MODE": "bogus", "AGENT_RUN_DISPATCH": ""})
    assert "ConfigurationError" in out and "AGENT_EXECUTION_MODE 非法" in out, out


@pytest.mark.unit
def test_production_rejects_inline_execution_mode():
    """生产不允许 inline（inline 无 durable、无崩溃恢复）。"""
    from core.config import validate_distributed_runtime_settings

    problems = validate_distributed_runtime_settings(
        dev_mode=False,
        session_backend="redis",
        checkpoint_backend="postgres",
        gunicorn_workers=1,
        lock_enabled=True,
        lock_backend="redis",
        agent_run_dispatch="inline",
    )
    assert any("celery" in p for p in problems), problems


@pytest.mark.unit
def test_production_allows_inline_only_in_dev():
    from core.config import validate_distributed_runtime_settings

    problems = validate_distributed_runtime_settings(
        dev_mode=True,
        session_backend="memory",
        checkpoint_backend="memory",
        gunicorn_workers=1,
        lock_enabled=True,
        lock_backend="memory",
        agent_run_dispatch="inline",
    )
    assert problems == [], problems


def test_module_exposes_canonical_symbol():
    """符号必须真正存在（不是只在文档里提到）。"""
    mod = importlib.import_module("core.config")
    assert hasattr(mod, "AGENT_EXECUTION_MODE")
    assert hasattr(mod, "AGENT_INLINE_COMPAT_ENDPOINTS")
    assert hasattr(mod, "AGENT_QUEUED_RUN_ENDPOINTS")
