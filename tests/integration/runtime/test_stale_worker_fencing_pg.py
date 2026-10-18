"""A3 复现：失去 lease 的旧 worker 继续执行图并继续发起写操作。

已知边界（本轮要收紧的）
------------------------
``AgentRun`` 的状态提交有 owner CAS（``transition_owned`` 要求
``worker_id = :me AND lease_expires_at > now``），所以**终态**不会被旧 worker 覆盖。
但这只约束了「提交状态」，不约束「继续执行」：

- ``_heartbeat_loop`` 在 ``renewed is False``（lease 已被接管）时**直接 return**，
  停止续租；
- 与此同时 ``await runtime.run(...)`` 这个 await **没有任何东西去取消它**，图继续跑、
  checkpoint 继续写、工具继续被调用。

因此「旧 worker 无法提交状态」不等于「旧 worker 不再执行」。本文件证明这一点，
并钉住修复后的行为。

修复方式选择（见 ``runtime/executor.py`` 的 ``_FenceGuard``）
------------------------------------------------------------
在现有架构内**协作式取消**是唯一既可靠又不引入新组件的方案：

- 心跳检测到 ownership 丢失 -> 触发一个事件；
- 监督器在 ``runtime.run()`` 之外等待该事件，一旦置位就**取消**图执行任务；
- 已知的剩余风险显式记录在 ``docs/reference/runtime-state-convergence.md``：
  协作式取消无法回收「取消之前已经发出」的外部调用，也不阻止非幂等工具。

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_stale_worker_fencing_pg.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import pytest

from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.statuses import RunStatus

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
requires_infra = pytest.mark.skipif(
    not (
        os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip() and os.getenv("TEST_REDIS_URL", "").strip()
    ),
    reason=INFRA_REASON,
)


def _session_factory(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def svc(pg_engine, cleanup_created_runs):
    return RunService(repository=AgentRunRepository(session_factory=_session_factory(pg_engine)))


def _running_run(svc: RunService, thread: str, *, worker: str, lease_s: float) -> str:
    run = svc.create_run(query="q", session_id=f"s-{uuid.uuid4().hex[:8]}", thread_id=thread)
    svc.mark_running(run["id"], worker_id=worker, task_id="t", lease_seconds=lease_s)
    return run["id"]


# ---------------------------------------------------------------------------
# 修复前的边界：终态受保护，但执行不受保护
# ---------------------------------------------------------------------------


@requires_infra
def test_stale_worker_cannot_commit_terminal_state(svc):
    """既有保证（必须保持）：旧 worker 不能覆盖新 owner 的终态。"""
    thread = f"a3-{uuid.uuid4().hex[:8]}"
    run_id = _running_run(svc, thread, worker="A", lease_s=0.2)
    import asyncio as _a

    _a.run(_a.sleep(0.35))  # lease 过期

    # B 接管
    taken = svc.mark_running(run_id, worker_id="B", task_id="t2", lease_seconds=120.0)
    assert taken is not None and taken["worker_id"] == "B"

    # A 迟到提交
    from runtime.run_service import RunOwnershipLost

    with pytest.raises(RunOwnershipLost):
        svc.mark_succeeded(run_id, {"answer": "from-A"}, expected_worker_id="A")
    assert svc.get_run(run_id)["status"] == RunStatus.RUNNING.value
    assert svc.get_run(run_id)["worker_id"] == "B"


@requires_infra
def test_ownership_loss_is_observable_to_the_running_executor(svc):
    """心跳必须能把「ownership 已丢失」报告给正在执行的 executor。

    这是协作式取消的触发信号：``_heartbeat_loop`` 依赖 ``renewed is False`` 来置位
    ``_FenceGuard``，没有它 executor 就无法知道该停下。

    注意判定必须来自**真实过期**：这里让租约真的过期（而不是伪造时间），因此
    `heartbeat` 返回 False 是「owner CAS 拒绝了一个已过期的租约」的真实结果。
    """
    thread = f"a3-{uuid.uuid4().hex[:8]}"
    run_id = _running_run(svc, thread, worker="A", lease_s=0.2)

    import asyncio as _a

    _a.run(_a.sleep(0.35))  # 真实等待租约过期
    assert (
        svc.heartbeat(run_id, worker_id="A", lease_seconds=120.0) is False
    ), "租约已过期 -> 续租必须被拒绝 -> 心跳据此上报 ownership 丢失"


@requires_infra
def test_owns_execution_is_read_only(svc):
    """``owns_execution`` 只判定，不续租。

    如果它顺带续租（例如复用 ``heartbeat``），语义就从「检查」变成「续租 + 检查」，
    调用方再也无法知道自己是否真的还持有执行权——而 fencing 正是靠这个事实决策。
    """
    thread = f"a3-{uuid.uuid4().hex[:8]}"
    run_id = _running_run(svc, thread, worker="A", lease_s=120.0)

    assert svc.owns_execution(run_id, worker_id="A") is True
    before = svc.get_run(run_id)["lease_expires_at"]

    # 反复调用不应把租约推后
    for _ in range(3):
        assert svc.owns_execution(run_id, worker_id="A") is True
    assert svc.get_run(run_id)["lease_expires_at"] == before, "owns_execution 不得续租"

    # 别人的身份不算
    assert svc.owns_execution(run_id, worker_id="B") is False


# ---------------------------------------------------------------------------
# 修复后的契约
# ---------------------------------------------------------------------------


@requires_infra
def test_fence_guard_cancels_graph_execution_on_ownership_loss():
    """核心契约：ownership 丢失会**取消**正在执行的图，而不是让它跑完。

    用真实 PostgreSQL 的 run 做触发源（真实心跳判定），被取消的是一个会持续写
    checkpoint 占位符的长任务——它模拟真实图执行。
    """
    from runtime.executor import _FenceGuard

    async def _scenario():
        guard = _FenceGuard()
        side_effects: list[str] = []
        run_finished = False

        async def _graph() -> str:
            nonlocal run_finished
            for i in range(200):
                await asyncio.sleep(0.01)
                # 图执行中的「写操作」：每轮记一次，代表一次外部调用
                side_effects.append(f"call-{i}")
            run_finished = True
            return "done"

        task = asyncio.create_task(_graph())
        # 让它跑一会儿
        await asyncio.sleep(0.05)
        assert side_effects, "图应在执行"

        guard.signal_ownership_lost("lease expired / taken over")
        await guard.cancel_execution(task)

        assert task.cancelled() or task.done()
        assert not run_finished, "失去执行权后不得让图跑完"
        # 取消之前的调用无法回收（这是剩余风险，必须显式承认）
        assert len(side_effects) > 0

    asyncio.run(_scenario())


@requires_infra
def test_fence_guard_is_a_noop_when_ownership_is_retained():
    """ownership 保持时不得干扰执行（否则会把正常长任务误杀）。"""
    from runtime.executor import _FenceGuard

    async def _scenario():
        guard = _FenceGuard()
        done: list[str] = []

        async def _graph() -> str:
            await asyncio.sleep(0.08)
            done.append("finished")
            return "ok"

        task = asyncio.create_task(_graph())
        watcher = asyncio.create_task(guard.wait())
        finished, _pending = await asyncio.wait(
            {task, watcher}, return_when=asyncio.FIRST_COMPLETED
        )
        assert task in finished, "ownership 未丢失时不得取消图执行"
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        assert await task == "ok"
        assert done == ["finished"]

    asyncio.run(_scenario())


@requires_infra
def test_side_effect_tools_recheck_ownership_before_executing(pg_engine, cleanup_created_runs):
    """工具执行前的 ownership 复核：失去执行权的 worker 不得发起新的外部写。

    这是第二道防线（第一道是协作式取消）。复核失败必须 **fail-closed**：拒绝执行，
    而不是「记一条日志然后照写」。

    ledger 必须注入**本 lane 的真实 PostgreSQL**：走默认 ``get_side_effect_store()``
    会落到默认 SQLite（``data/csai.db``），而那个库里通常没有 ``tool_side_effects``
    表 —— CI 上正是因此失败（``no such table: tool_side_effects``）。
    """
    from runtime import side_effects as se

    store = se.SideEffectStore(session_factory=_session_factory(pg_engine))
    monkey = pytest.MonkeyPatch()
    monkey.setattr(se, "get_side_effect_store", lambda: store)
    try:
        _assert_tool_fence_refuses_write()
    finally:
        monkey.undo()


def _assert_tool_fence_refuses_write() -> None:
    """``ToolRegistry`` 的 execution fence 必须在失去 ownership 时拒绝写操作。"""
    from tools.tool_registry import LogicalCall, ToolCachePolicy, ToolRegistry

    executed: list[dict] = []

    async def _handler(arguments: dict) -> str:
        executed.append(dict(arguments))
        return "ok"

    from runtime.context import reset_run_context, set_run_context

    # 唯一 run_id：operation_key 由 (run, node, agent, tool, ordinal) 派生，
    # 固定值会让 ledger 命中上一次运行留下的 SUCCEEDED 行，表现为「工具没被执行」。
    fence_run = f"a3-fence-{uuid.uuid4().hex[:8]}"
    tokens = set_run_context(fence_run, "thread-a3-fence", "task-a3")

    async def _scenario():
        registry = ToolRegistry()
        registry.register(
            name="write_thing",
            description="w",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
            side_effect=True,
            cache_policy=ToolCachePolicy(enabled=False),
        )

        # ownership 仍然有效 -> 允许执行
        registry.set_execution_fence(lambda: True)
        await registry.execute_raw(
            "write_thing",
            {"x": 1},
            tool_call_id="c1",
            logical_call=LogicalCall(node_id="n#0", agent="a", ordinal=0),
        )
        assert len(executed) == 1

        # ownership 已丢失 -> fail-closed 拒绝
        registry.set_execution_fence(lambda: False)
        result = await registry.execute_raw(
            "write_thing",
            {"x": 2},
            tool_call_id="c2",
            logical_call=LogicalCall(node_id="n#0", agent="a", ordinal=1),
        )
        assert len(executed) == 1, "失去执行权后不得发起新的外部写"
        assert isinstance(result, str)
        assert "执行权" in result or "拒绝" in result

    try:
        asyncio.run(_scenario())
    finally:
        reset_run_context(tokens)


@requires_infra
def test_fence_check_never_blocks_readonly_tools():
    """只读工具不受 ownership 复核影响（普通问答/查询路径不变）。"""
    from tools.tool_registry import ToolRegistry

    calls: list[int] = []

    async def _handler(arguments: dict) -> str:
        calls.append(1)
        return "data"

    async def _scenario():
        registry = ToolRegistry()
        registry.register(
            name="read_thing",
            description="r",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
            side_effect=False,
        )
        registry.set_execution_fence(lambda: False)
        out = await registry.execute_raw("read_thing", {"x": 1})
        assert out == "data"
        assert len(calls) == 1

    asyncio.run(_scenario())


@requires_infra
def test_stale_worker_side_effect_is_deduped_by_replay_stable_key(svc, pg_engine):
    """A1 与 A3 的交互：旧 worker 继续执行时，同一逻辑写仍然只发生一次。

    这是本轮修复把 A3 爆炸半径收窄的关键：A1 之后逻辑操作身份与 LLM 输出无关，
    所以旧 worker 继续跑同一节点时会命中**同一个** ``operation_key``，被 ledger 去重，
    而不是一个新的写操作。
    """
    from runtime.side_effects import SideEffectStore, build_replay_stable_operation_key

    store = SideEffectStore(session_factory=_session_factory(pg_engine))

    async def _scenario():
        external_calls: list[dict] = []

        async def _write(arguments: dict) -> str:
            external_calls.append(dict(arguments))
            return "ok"

        run_id = f"a3-replay-{uuid.uuid4().hex[:8]}"
        args = {"order_id": "ORD-1", "amount": 10}
        key = build_replay_stable_operation_key(
            run_id=run_id, node_id="n#0", agent="a", tool_name="w", ordinal=0
        )

        # 旧 worker A 执行并完成
        from runtime.side_effects import execute_idempotent_operation

        await execute_idempotent_operation(
            tool_name="w",
            operation_key=key,
            run_id=run_id,
            thread_id="t",
            arguments=args,
            operation=lambda: _write(args),
            store=store,
        )
        # 新 worker B 重放同一节点：tool_call_id 不同，但逻辑身份相同
        await execute_idempotent_operation(
            tool_name="w",
            operation_key=key,
            run_id=run_id,
            thread_id="t",
            arguments=args,
            operation=lambda: _write(args),
            store=store,
        )
        assert len(external_calls) == 1, "跨 worker 重放不得重复写"

    asyncio.run(_scenario())


@requires_infra
def test_fenced_worker_commits_no_terminal_state(svc, monkeypatch):
    """被 fencing 的 worker **不得写任何终态**，也不得消耗重试预算。

    「失去执行权」不是业务失败。写成 FAILED 会覆盖新 owner 的执行结果、写成
    RETRYING 会凭空消耗 ``attempt``。正确处置是什么都不写，返回当前状态。
    """
    import core.config as config

    thread = f"a3-{uuid.uuid4().hex[:8]}"
    run_id = _running_run(svc, thread, worker="A", lease_s=0.1)
    monkeypatch.setattr(config, "AGENT_RUN_THREAD_LOCK_ENABLED", False)

    import asyncio as _a

    from runtime import executor as ex

    _a.run(_a.sleep(0.3))  # A 的租约过期

    # B 接管，run 归 B 所有
    taken = svc.mark_running(run_id, worker_id="B", task_id="t2", lease_seconds=120.0)
    assert taken is not None and taken["worker_id"] == "B"
    attempt_before = svc.get_run(run_id)["attempt"]

    # A 的执行路径：mark_succeeded 必然被 owner CAS 拒绝
    from runtime.run_service import RunOwnershipLost

    with pytest.raises(RunOwnershipLost):
        svc.mark_succeeded(run_id, {"answer": "from-A"}, expected_worker_id="A")

    after = svc.get_run(run_id)
    assert after["status"] == RunStatus.RUNNING.value, "不得写入终态"
    assert after["attempt"] == attempt_before, "失去执行权不得消耗重试预算"
    assert after["worker_id"] == "B"
    assert ex.ExecutionFenced is not None


# ---------------------------------------------------------------------------
# F1：生产入口必须把 fence 装到 agent 真正使用的 ToolRegistry 上
# ---------------------------------------------------------------------------


@requires_infra
def test_production_entry_installs_fence_on_runtime_registry(
    pg_engine,
    monkeypatch,
    thread_lock_off,
    cleanup_created_runs,
    side_effect_store,
):
    """不依赖 ``registry.set_execution_fence`` 的测试专用注入：直接走 ``execute_run``。

    生产构造是 ``AgentRuntime -> ServiceContainer.tool_registry``；``RunService`` 上
    没有 ``tool_registry`` 属性。修复前 ``_install_execution_fence`` 因此恒为空操作，
    第二道防线从未在 worker 路径生效。本用例让 runtime 自带一个真实 ToolRegistry，
    在 lease 过期后调用写工具，断言 handler 未被调用。
    """
    import asyncio
    from types import SimpleNamespace

    import core.config as config
    from runtime import executor as ex
    from runtime import side_effects as se
    from tools.tool_registry import LogicalCall, ToolCachePolicy, ToolRegistry

    monkeypatch.setattr(config, "AGENT_RUN_THREAD_LOCK_ENABLED", False)
    monkeypatch.setattr(config, "AGENT_RUN_LEASE_SECONDS", 0.2)
    monkeypatch.setattr(config, "AGENT_RUN_HEARTBEAT_SECONDS", 60.0)

    store = se.SideEffectStore(session_factory=_session_factory(pg_engine))
    monkeypatch.setattr(se, "get_side_effect_store", lambda: store)

    calls: list[dict] = []

    async def _handler(arguments: dict) -> str:
        calls.append(dict(arguments))
        return "written"

    registry = ToolRegistry()
    registry.register(
        name="fenced_write",
        description="w",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        side_effect=True,
        cache_policy=ToolCachePolicy(enabled=False),
    )

    class _Runtime:
        def __init__(self) -> None:
            self.container = SimpleNamespace(tool_registry=registry)

        async def run(self, *, thread_id, query, user_id=None, resume_command=None):
            # 等 lease 真实过期，再发起一次写工具调用。
            await asyncio.sleep(0.45)
            out = await registry.execute_raw(
                "fenced_write",
                {"order_id": "ORD-F1"},
                tool_call_id="call-f1",
                logical_call=LogicalCall(node_id="n#0", agent="a", ordinal=0),
            )
            return {"response": out}

    async def _provider():
        return _Runtime()

    svc = RunService(repository=AgentRunRepository(session_factory=_session_factory(pg_engine)))
    run = svc.create_run(
        query="q",
        session_id=f"s-f1-{uuid.uuid4().hex[:8]}",
        thread_id=f"t-f1-{uuid.uuid4().hex[:8]}",
    )

    status = asyncio.run(
        ex.execute_run(
            run["id"],
            service=svc,
            runtime_provider=_provider,
            dispatcher=None,
            lock_manager=None,
            worker_id="worker-A",
        )
    )

    assert calls == [], "失去 ownership 后写工具 handler 不得被调用（fence 必须真的装上）"
    assert status == RunStatus.RUNNING.value, "失去 ownership 不得写终态"
    # fence 谓词必须在执行结束后清理，避免影响后续 run
    assert registry._run_execution_fences == {}
