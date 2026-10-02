"""executor 审批挂起/恢复路径测试（确定性，无外部依赖）。

锁住三件事：

1. 图挂在 ``__interrupt__`` 上时，run 转 ``WAITING_APPROVAL``——**不是**成功、
   也**不是**失败（不进 retry / DLQ）；
2. 恢复前若没有可消费的决策，run 保持 ``WAITING_APPROVAL``，不被误标成功；
3. 审批恢复**不递增 attempt**（等待人不是失败，不该消耗重试预算）。
"""

from __future__ import annotations

import pytest

from runtime.executor import _awaiting_approval, _runtime_accepts_resume, execute_run
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.statuses import RunStatus
from tests.unit.runtime_helpers import (
    FakeRuntime,
    RecordingDispatcher,
    dispose,
    make_sqlite_session_factory,
    runtime_provider,
)


@pytest.fixture
def env():
    session_factory, engine, path = make_sqlite_session_factory()
    repo = AgentRunRepository(session_factory=session_factory)
    service = RunService(repository=repo)
    try:
        yield service, repo
    finally:
        dispose(engine, path)


class _ApprovalRuntime:
    """模拟图挂在 interrupt 上的 runtime。"""

    def __init__(self):
        self.resume_commands = []

    async def run(
        self,
        *,
        thread_id: str,
        query: str,
        user_id: str | None = None,
        resume_command=None,
    ):
        self.resume_commands.append(resume_command)
        return {"__interrupt__": [{"value": {"action": "staging_refund"}}], "response": ""}


class TestAwaitingApproval:
    def test_detects_interrupt(self):
        assert _awaiting_approval({"__interrupt__": [object()]}) is True

    def test_plain_result_is_not_awaiting(self):
        assert _awaiting_approval({"response": "done"}) is False

    def test_non_dict(self):
        assert _awaiting_approval(None) is False
        assert _awaiting_approval("x") is False


class TestRuntimeAcceptsResume:
    def test_legacy_runtime_without_kwarg(self):
        """既有实现按固定签名实现 run() -> 判定为不支持。"""

        class Legacy:
            async def run(self, *, thread_id, query, user_id=None):
                return {}

        assert _runtime_accepts_resume(Legacy().run) is False

    def test_runtime_with_kwarg(self):
        class Modern:
            async def run(self, *, thread_id, query, user_id=None, resume_command=None):
                return {}

        assert _runtime_accepts_resume(Modern().run) is True

    def test_var_kwargs_counts_as_supporting(self):
        class Flex:
            async def run(self, **kwargs):
                return {}

        assert _runtime_accepts_resume(Flex().run) is True

    def test_agent_runtime_supports_resume(self):
        from runtime.bootstrap import AgentRuntime

        assert _runtime_accepts_resume(AgentRuntime.run) is True


class TestParkForApproval:
    def test_run_parked_not_succeeded(self, env):
        service, repo = env
        run = service.create_run(
            query="退款",
            session_id="T-hitl-1",
            status=RunStatus.QUEUED,
        )
        run_id = run["id"]

        runtime = _ApprovalRuntime()
        status = _run(execute_run, service, run_id, runtime)

        assert status == RunStatus.WAITING_APPROVAL.value
        assert service.get_run(run_id)["status"] == RunStatus.WAITING_APPROVAL.value

    def test_park_clears_lease(self, env):
        """图已挂起、没有 worker 在执行 -> lease 必须清空，否则接管判断失真。"""
        service, repo = env
        run = service.create_run(
            query="退款",
            session_id="T-hitl-2",
            status=RunStatus.QUEUED,
        )
        run_id = run["id"]

        _run(execute_run, service, run_id, _ApprovalRuntime())

        stored = service.get_run(run_id)
        assert stored["lease_expires_at"] is None
        assert stored["worker_id"] is None
        # 对照：确实曾经被领取过（有 heartbeat/start 痕迹）
        assert stored["started_at"] is not None

    def test_park_does_not_consume_attempt(self, env):
        """挂起不是失败：attempt 只因「领取」+1，不因「挂起」再 +1。"""
        service, repo = env
        run = service.create_run(
            query="退款",
            session_id="T-hitl-3",
            status=RunStatus.QUEUED,
        )
        run_id = run["id"]
        assert run["attempt"] == 0
        _run(execute_run, service, run_id, _ApprovalRuntime())
        # 领取一次 -> 1；WAITING_APPROVAL 迁移没有再 +1。
        assert service.get_run(run_id)["attempt"] == 1

    def test_no_resume_command_keeps_waiting(self, env, monkeypatch):
        """没有可消费的决策 -> 保持 WAITING_APPROVAL，不误标成功、不消耗重试。"""
        service, repo = env
        run = service.create_run(
            query="退款",
            session_id="T-hitl-4",
            status=RunStatus.QUEUED,
        )
        run_id = run["id"]
        # 第一阶段：图挂起 -> WAITING_APPROVAL
        _run(execute_run, service, run_id, _ApprovalRuntime())
        assert service.get_run(run_id)["status"] == RunStatus.WAITING_APPROVAL.value

        # 第二阶段：投递了，但没有可消费的审批决策（仍在 PENDING）
        monkeypatch.setattr("runtime.executor._build_resume_command", lambda rid: None)

        status = _run(execute_run, service, run_id, _ApprovalRuntime())
        assert status == RunStatus.WAITING_APPROVAL.value
        assert service.get_run(run_id)["status"] == RunStatus.WAITING_APPROVAL.value

    def test_resume_does_not_increment_attempt(self, env, monkeypatch):
        """恢复执行**不**递增 attempt：等人审批不是失败重试。"""
        service, repo = env
        run = service.create_run(
            query="退款",
            session_id="T-hitl-5",
            status=RunStatus.QUEUED,
        )
        run_id = run["id"]
        # 第一阶段：图挂起
        _run(execute_run, service, run_id, _ApprovalRuntime())
        attempt_before = service.get_run(run_id)["attempt"]

        sentinel = object()
        monkeypatch.setattr("runtime.executor._build_resume_command", lambda rid: sentinel)

        # 恢复后图正常完成（无 interrupt）。
        ok_runtime = FakeRuntime(lambda **kw: {"response": "已退款"})

        class _Accepts:
            def __init__(self, inner):
                self.inner = inner
                self.seen = []

            async def run(self, *, thread_id, query, user_id=None, resume_command=None):
                self.seen.append(resume_command)
                return await self.inner.run(thread_id=thread_id, query=query, user_id=user_id)

        wrapped = _Accepts(ok_runtime)
        status = _run(execute_run, service, run_id, wrapped)

        assert wrapped.seen == [sentinel]
        assert status == RunStatus.SUCCEEDED.value
        assert service.get_run(run_id)["attempt"] == attempt_before

    def test_runtime_without_resume_support_fails_loudly(self, env, monkeypatch):
        """注入的 runtime 不支持 resume_command 时必须响亮失败，而不是静默丢弃审批。"""
        service, repo = env
        run = service.create_run(
            query="退款",
            session_id="T-hitl-6",
            status=RunStatus.QUEUED,
        )
        run_id = run["id"]
        # 第一阶段：图挂起
        _run(execute_run, service, run_id, _ApprovalRuntime())

        monkeypatch.setattr("runtime.executor._build_resume_command", lambda rid: object())

        legacy = FakeRuntime(lambda **kw: {"response": "should not run"})

        status = _run(execute_run, service, run_id, legacy)
        # 走到 TypeError -> permanent failure，可观测（而不是静默挂起）
        assert status in (RunStatus.FAILED.value, RunStatus.RETRYING.value)
        assert service.get_run(run_id)["status"] != RunStatus.SUCCEEDED.value


def _run(coro_fn, service, run_id, runtime):
    """同步驱动 execute_run（Celery 任务的可执行内核）。"""
    import asyncio

    return asyncio.run(
        coro_fn(
            run_id,
            service=service,
            runtime_provider=runtime_provider(runtime),
            dispatcher=RecordingDispatcher(),
        )
    )
