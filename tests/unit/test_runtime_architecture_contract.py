"""Runtime architecture contract tests.

这些测试锁定「分布式运行时」的关键不变量，防止后续单边修改导致 API 与
Worker 行为不一致、或生产配置静默退回进程内状态。

Contract 1: 生产 + GUNICORN_WORKERS>1 必须 postgres checkpoint + redis session
            + redis per-thread lock。
Contract 2: API 执行边界与 Worker 使用**同一** thread lock manager 单例与 key namespace。
Contract 3: 生产不允许 MemorySaver / session memory / lock disabled /
            AGENT_RUN_DISPATCH=inline。
Contract 4: thread lock TTL 必须 > task time limit + safety margin。
"""

from __future__ import annotations

import asyncio

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
def test_api_and_worker_share_one_lock_manager_instance():
    """API 执行边界与 worker 必须拿到**同一个** lock manager 实例。

    两套实例 = 两套锁状态：后端降级为 memory 的 DEV/TEST 下，API 快路径与 worker
    路径会各持一份互不相干的登记表，跨路径并发写同一 thread 无法被发现。
    """
    from core.concurrency import distributed_lock as api_lock
    from runtime import thread_lock as worker_lock

    api_lock.reset_api_lock_manager_for_tests()
    worker_lock.reset_thread_lock_manager_for_tests()
    try:
        api_manager = api_lock.get_api_lock_manager()
        worker_manager = worker_lock.get_thread_lock_manager()
        assert api_manager is not None
        assert api_manager is worker_manager
        # 复位后应重新解析为同一实例（幂等，不因调用次数产生新对象）
        assert api_lock.get_api_lock_manager() is worker_lock.get_thread_lock_manager()
    finally:
        api_lock.reset_api_lock_manager_for_tests()
        worker_lock.reset_thread_lock_manager_for_tests()


@pytest.mark.unit
def test_api_lock_actually_serializes_against_worker_path():
    """行为级：worker 侧持锁时 API 边界必须观测到 THREAD_BUSY。

    用同一个共享 manager 先占锁，再走 API 的 ``thread_lock`` 上下文。
    """
    from core.concurrency import distributed_lock as api_lock
    from runtime import thread_lock as worker_lock

    api_lock.reset_api_lock_manager_for_tests()
    worker_lock.reset_thread_lock_manager_for_tests()
    try:
        manager = worker_lock.get_thread_lock_manager()
        thread_id = "shared-manager-contract"

        async def scenario():
            # worker 路径持锁
            got = await manager.acquire(thread_id, "worker-owner", 30.0)
            assert got is True
            try:
                with pytest.raises(api_lock.ThreadBusyError):
                    async with api_lock.thread_lock(thread_id, acquire_timeout=0.2):
                        pytest.fail("API 不应在 worker 持锁时进入临界区")
            finally:
                await manager.release(thread_id, "worker-owner")
            # 释放后 API 必须能拿到锁
            async with api_lock.thread_lock(thread_id, acquire_timeout=1.0) as owner:
                assert owner

        asyncio.run(scenario())
    finally:
        api_lock.reset_api_lock_manager_for_tests()
        worker_lock.reset_thread_lock_manager_for_tests()


# ---------------------------------------------------------------------------
# Contract 3: production must not use process-local fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_production_rejects_memory_checkpointer():
    """生产必须拒绝 MemorySaver，无论 gunicorn worker 数量。

    注意：这条规则由 ``validate_checkpoint_settings`` 强制（与 worker 数量无关），
    ``validate_distributed_runtime_settings`` 只在多 worker 维度重复要求一次。
    """
    from core.config import validate_checkpoint_settings

    problems = validate_checkpoint_settings("memory", dev_mode=False)
    assert any("LANGGRAPH_CHECKPOINT_BACKEND=postgres" in p for p in problems), problems

    # postgres 但没有可解析的 PG DSN -> 也必须被拒
    problems = validate_checkpoint_settings("postgres", dev_mode=False, database_url="")
    assert any("PostgreSQL" in p for p in problems), problems

    # 正确配置通过
    assert (
        validate_checkpoint_settings(
            "postgres",
            dev_mode=False,
            explicit_url="postgresql://u:p@db:5432/x",
        )
        == []
    )

    # dev 允许 memory
    assert validate_checkpoint_settings("memory", dev_mode=True) == []

    # 分布式一致性 gate 在多 worker 下同样要求 postgres
    problems = validate_distributed_runtime_settings(
        dev_mode=False,
        session_backend="redis",
        checkpoint_backend="memory",
        gunicorn_workers=4,
        lock_enabled=True,
        lock_backend="redis",
    )
    assert any("LANGGRAPH_CHECKPOINT_BACKEND=postgres" in p for p in problems), problems


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
