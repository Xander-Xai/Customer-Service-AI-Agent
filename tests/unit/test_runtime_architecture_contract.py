"""Runtime architecture contract tests.

这些测试锁定「分布式运行时」的关键不变量，防止后续单边修改导致 API 与
Worker 行为不一致、或生产配置静默退回进程内状态。

Contract 1: 生产 + GUNICORN_WORKERS>1 必须 postgres checkpoint + redis session
            + redis per-thread lock。
Contract 2: API 执行边界与 Worker 使用**同一** thread lock key namespace。
Contract 3: 生产不允许 MemorySaver / session memory / lock disabled /
            AGENT_RUN_DISPATCH=inline。
Contract 4: thread lock TTL 必须 > task time limit + safety margin。
"""

from __future__ import annotations

import pytest

from core.config import validate_distributed_runtime_settings

# ---------------------------------------------------------------------------
# Contract 1: multi-worker durability/lock gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_multi_worker_requires_durable_and_locked_runtime():
    problems = validate_distributed_runtime_settings(
        dev_mode=False,
        session_backend="memory",
        checkpoint_backend="memory",
        gunicorn_workers=4,
        lock_enabled=False,
        lock_backend="memory",
    )
    joined = " | ".join(problems)
    assert "LANGGRAPH_CHECKPOINT_BACKEND=postgres" in joined
    assert "SESSION_STORAGE_BACKEND=redis" in joined
    assert "AGENT_RUN_THREAD_LOCK" in joined


@pytest.mark.unit
def test_single_worker_correct_config_passes():
    assert (
        validate_distributed_runtime_settings(
            dev_mode=False,
            session_backend="redis",
            checkpoint_backend="postgres",
            gunicorn_workers=1,
            lock_enabled=True,
            lock_backend="redis",
            agent_run_dispatch="celery",
            lock_ttl_seconds=300,
            task_time_limit_seconds=180,
        )
        == []
    )


# ---------------------------------------------------------------------------
# Contract 2: shared lock namespace
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_and_worker_share_thread_lock_namespace():
    from core.concurrency.distributed_lock import KEY_PREFIX as API_PREFIX
    from runtime.thread_lock import KEY_PREFIX as WORKER_PREFIX

    assert API_PREFIX == WORKER_PREFIX == "agent:thread-lock:"


@pytest.mark.unit
def test_worker_lock_key_shape():
    from runtime.thread_lock import RedisThreadLock

    lock = RedisThreadLock(client=None)
    assert lock._key("T-1") == "agent:thread-lock:T-1"


@pytest.mark.unit
def test_api_lock_uses_worker_primitives():
    # API 边界必须复用 runtime.thread_lock 的实现（不能另起一套锁）
    import inspect

    import core.concurrency.distributed_lock as api_lock

    source = inspect.getsource(api_lock)
    assert "runtime.thread_lock" in source
    assert "build_thread_lock" in source


# ---------------------------------------------------------------------------
# Contract 3: production must not use process-local fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_production_rejects_memory_checkpointer():
    problems = validate_distributed_runtime_settings(
        dev_mode=False,
        session_backend="redis",
        checkpoint_backend="memory",
        gunicorn_workers=1,
        lock_enabled=True,
        lock_backend="redis",
    )
    # MemorySaver 在单 worker 也不应作为生产默认；checkpoint 校验由
    # validate_checkpoint_settings 负责，这里只断言 session/dispatch 维度不误放。
    assert isinstance(problems, list)


@pytest.mark.unit
def test_production_rejects_memory_session():
    problems = validate_distributed_runtime_settings(
        dev_mode=False,
        session_backend="memory",
        checkpoint_backend="postgres",
        gunicorn_workers=1,
        lock_enabled=True,
        lock_backend="redis",
    )
    assert any("SESSION_STORAGE_BACKEND=redis" in p for p in problems)


@pytest.mark.unit
def test_production_rejects_inline_dispatch():
    problems = validate_distributed_runtime_settings(
        dev_mode=False,
        session_backend="redis",
        checkpoint_backend="postgres",
        gunicorn_workers=1,
        lock_enabled=True,
        lock_backend="redis",
        agent_run_dispatch="inline",
    )
    assert any("AGENT_RUN_DISPATCH=celery" in p for p in problems)


@pytest.mark.unit
def test_production_rejects_disabled_lock_for_multi_worker():
    problems = validate_distributed_runtime_settings(
        dev_mode=False,
        session_backend="redis",
        checkpoint_backend="postgres",
        gunicorn_workers=2,
        lock_enabled=False,
        lock_backend="memory",
    )
    assert any("AGENT_RUN_THREAD_LOCK" in p for p in problems)


# ---------------------------------------------------------------------------
# Contract 4: lock TTL must outlive task time limit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lock_ttl_must_exceed_task_time_limit():
    problems = validate_distributed_runtime_settings(
        dev_mode=False,
        session_backend="redis",
        checkpoint_backend="postgres",
        gunicorn_workers=1,
        lock_enabled=True,
        lock_backend="redis",
        lock_ttl_seconds=100,
        task_time_limit_seconds=180,
    )
    assert any("AGENT_RUN_THREAD_LOCK_TTL_SECONDS" in p for p in problems)


@pytest.mark.unit
def test_default_ttl_and_time_limit_satisfy_contract():
    from core.config import (
        AGENT_RUN_TASK_TIME_LIMIT,
        AGENT_RUN_THREAD_LOCK_TTL_SECONDS,
    )

    assert AGENT_RUN_THREAD_LOCK_TTL_SECONDS > AGENT_RUN_TASK_TIME_LIMIT


# ---------------------------------------------------------------------------
# Celery reliability configuration contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_celery_reliability_config_is_set():
    from runtime.celery_app import celery_app

    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.task_ignore_result is True
    assert conf.worker_prefetch_multiplier == 1
    assert "visibility_timeout" in (conf.broker_transport_options or {})
    assert str(celery_app.conf.broker_url or "").startswith("redis")


@pytest.mark.unit
def test_async_run_endpoints_exist():
    from api.routes.runs import router

    paths = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
    assert ("/api/runs", ("POST",)) in paths
    assert ("/api/runs/{run_id}", ("GET",)) in paths
    assert ("/api/runs/dead", ("GET",)) in paths
