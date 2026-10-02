"""P0/P1 运行时语义集成测试（真实 PostgreSQL + 真实 Redis，非 mock）。

覆盖验收 Gate：

  - Gate 2  thread/run 分离：同一会话多次 run 共享 thread_id，run_id 各自唯一；
  - Gate 3  同 thread 不并发：用**真实 graph 执行区间**（started_at/finished_at）
           证明不重叠，而不只是"Redis SETNX 成功"；
  - Gate 4  不同 thread 可并发：整体耗时接近单次 sleep，不是两倍；
  - Gate 5  lease 崩溃安全：非 owner 不能释放；owner 崩溃 + TTL 过期后可被接管；
  - Gate 8  transient 重试：两次失败后第三次成功 -> SUCCEEDED，attempt=3；
  - Gate 9  permanent 错误（401/403/参数非法）立即 FAILED，不重试；
  - Gate 10 DLQ + 重放：达到 max_attempts -> DEAD_LETTER 且证据齐全，CLI 可重放；
  - Gate 12 run 查询：字段齐全且不泄露 secret。

运行::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_run_semantics.py -q
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pytest

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
requires_infra = pytest.mark.skipif(
    not (os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip() and os.getenv("TEST_REDIS_URL", "").strip()),
    reason=INFRA_REASON,
)

pytestmark = [pytest.mark.slow, requires_infra]

_SLEEP = 1.5


class HttpStatusError(Exception):
    """带 status_code 的异常，用于验证 4xx 分类（401/403 -> permanent）。"""

    def __init__(self, status: int):
        self.status_code = status
        super().__init__(f"HTTP {status}")


class _HttpStatusError(HttpStatusError):
    """兼容旧引用。"""


def _permanent(message: str):
    """业务校验/不支持类错误的**生产契约**：工具层抛 ``PermanentError``（不重试）。

    注意 ``classify_exception`` 对完全未知的异常默认判为 transient（可重试）：
    未识别异常可能是网络/上游抖动，直接判 permanent 会误伤。
    """
    from runtime.errors import PermanentError

    return PermanentError(message)


def _clone(exc: Exception) -> Exception:
    """每次生成全新异常实例（traceback 不可跨 run 复用）。"""
    if isinstance(exc, HttpStatusError):
        return type(exc)(exc.status_code)
    return type(exc)(str(exc))


class _RecordingRuntime:
    """记录真实执行区间与临界区重叠数的 runtime 替身。

    只替换"图执行"，**不**替换 thread lock / lease / 状态机 —— 那些正是被测对象。
    """

    def __init__(self, sleep: float = _SLEEP, fail_times: int = 0, exc_factory=None):
        self.sleep = sleep
        self.fail_times = fail_times
        self._exc_factory = exc_factory
        self.intervals: list[tuple[str, float, float]] = []
        self.calls: list[str] = []

    async def run(self, *, thread_id: str, query: str, user_id: str | None = None, **kw: Any):
        started = time.monotonic()
        self.calls.append(thread_id)
        try:
            if self.fail_times > 0:
                self.fail_times -= 1
                exc = (self._exc_factory or (lambda: TimeoutError("transient boom")))()
                raise exc
            await asyncio.sleep(self.sleep)
            return {"response": f"ok:{query}", "thread_id": thread_id}
        finally:
            self.intervals.append((thread_id, started, time.monotonic()))

    def max_overlap(self) -> int:
        """任一时刻最多有几个同 thread 执行同时处于临界区（必须 == 1）。"""
        events: list[tuple[float, int]] = []
        for _tid, s, e in self.intervals:
            events.append((s, 1))
            events.append((e, -1))
        events.sort()
        cur = peak = 0
        for _t, delta in events:
            cur += delta
            peak = max(peak, cur)
        return peak


def _redis_lock(redis_url: str, prefix: str):
    import redis.asyncio as aioredis

    from runtime.thread_lock import RedisThreadLock

    client = aioredis.from_url(redis_url, decode_responses=True, socket_timeout=5)
    return RedisThreadLock(client, key_prefix=prefix)


# ---------------------------------------------------------------------------
# Gate 2: thread_id / run_id 分离
# ---------------------------------------------------------------------------


def test_gate2_thread_and_run_are_separated(run_service, unique):
    """同一会话连续 3 次执行：thread_id 相同，run_id 互不相同，且 DB 里能分别查到。

    「一个请求一个新 thread」= FAIL。
    """
    session_id = unique("T-gate2")

    runs = [
        run_service.create_run(query=f"q{i}", session_id=session_id, user_id="u1")
        for i in range(3)
    ]
    run_ids = [r["id"] for r in runs]

    assert len(set(run_ids)) == 3, "run_id 必须互不相同"
    assert all(r["thread_id"] == session_id for r in runs), (
        "同一会话的多次 run 必须共享同一 thread_id"
    )

    # 全部能从数据库分别查回
    for rid, created in zip(run_ids, runs, strict=True):
        fetched = run_service.require_run(rid)
        assert fetched["id"] == rid
        assert fetched["thread_id"] == created["thread_id"]
        assert fetched["query"] == created["query"]


def test_gate2_idempotency_scope_cannot_overflow_column(run_service, unique):
    """超长 Idempotency-Key 不得写入失败（列宽 128），且仍按 user 隔离。"""
    long_key = "k" * 4000
    a = run_service.create_run(
        query="q",
        session_id=unique("T-idem"),
        user_id="alice",
        idempotency_key=run_service_module().build_idempotency_scope("alice", "POST:/api/runs", long_key),
    )
    b = run_service.create_run(
        query="q",
        session_id=unique("T-idem"),
        user_id="bob",
        idempotency_key=run_service_module().build_idempotency_scope("bob", "POST:/api/runs", long_key),
    )
    assert a["id"] != b["id"], "不同 user 的同名原始 key 必须互相隔离"
    assert len(a["idempotency_key"]) <= 128
    assert len(b["idempotency_key"]) <= 128


def run_service_module():
    import runtime.run_service as mod

    return mod


# ---------------------------------------------------------------------------
# Gate 3: 同 thread 串行（真实执行区间不重叠）
# ---------------------------------------------------------------------------


def test_gate3_same_thread_never_overlaps(run_service, redis_url, unique, monkeypatch):
    """两个 run 落在同一 thread：必须串行，执行区间不重叠。

    证据不只是锁拿到没拿到，而是 **graph 实际执行时间区间**：
    B.started_at >= A.finished_at。
    """
    from runtime.executor import execute_run
    from runtime.thread_lock import reset_thread_lock_manager_for_tests

    reset_thread_lock_manager_for_tests()
    import core.config as config

    monkeypatch.setattr(config, "AGENT_RUN_THREAD_LOCK_BACKEND", "redis")
    monkeypatch.setattr(config, "AGENT_RUN_THREAD_LOCK_TTL_SECONDS", 30.0)
    monkeypatch.setattr(config, "AGENT_RUN_THREAD_LOCK_RETRY_DELAY_SECONDS", 0.2)
    monkeypatch.setattr(config, "AGENT_RUN_LEASE_SECONDS", 60.0)

    thread_id = unique("T-gate3")
    a = run_service.create_run(query="A", session_id=thread_id)
    b = run_service.create_run(query="B", session_id=thread_id)

    runtime = _RecordingRuntime(sleep=_SLEEP)
    dispatched: list[tuple[str, float | None]] = []

    async def dispatcher(run_id, countdown=None):
        dispatched.append((run_id, countdown))
        return "task-stub"

    async def run_once(run_id: str, worker: str):
        return await execute_run(
            run_id,
            service=run_service,
            runtime_provider=lambda: _ready(runtime),
            dispatcher=dispatcher,
            worker_id=worker,
        )

    async def scenario():
        # A、B **同时**投递：必须由 thread lock 决定谁先跑，另一个被延迟重调度。
        first, second = await asyncio.gather(
            run_once(a["id"], "w-a"), run_once(b["id"], "w-b")
        )
        return first, second

    first_status, second_status = asyncio.run(scenario())

    # 竞争确实发生过：落败方被延迟重调度（保持 QUEUED，等待重新投递）
    assert dispatched, "同 thread 并发投递时应有一个 run 被延迟重调度"
    deferred_id = dispatched[0][0]
    deferred_status = first_status if deferred_id == a["id"] else second_status
    assert deferred_status == "QUEUED", (
        f"落败方应保持 QUEUED 等待重投，实际 {deferred_status}"
    )

    # 模拟 broker 的 countdown 重投，直到两个 run 都进入终态
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        pending = [
            rid
            for rid in (a["id"], b["id"])
            if run_service.require_run(rid)["status"] not in ("SUCCEEDED", "FAILED")
        ]
        if not pending:
            break
        for rid in pending:
            asyncio.run(run_once(rid, f"w-retry-{rid[:6]}"))
        time.sleep(0.2)

    reset_thread_lock_manager_for_tests()

    row_a = run_service.require_run(a["id"])
    row_b = run_service.require_run(b["id"])
    assert row_a["status"] == "SUCCEEDED", row_a
    assert row_b["status"] == "SUCCEEDED", row_b

    # 真正的证据：DB 时间戳区间不重叠
    assert row_a["started_at"] is not None and row_a["finished_at"] is not None
    assert row_b["started_at"] is not None and row_b["finished_at"] is not None
    assert row_a["finished_at"] <= row_b["started_at"], (
        f"执行区间重叠: A={row_a['started_at']}..{row_a['finished_at']} "
        f"B={row_b['started_at']}..{row_b['finished_at']}"
    )

    # 旁证：进程内观察到的最大同时执行数 == 1（同 thread）
    by_thread: dict[str, list[tuple[float, float]]] = {}
    for tid, s, e in runtime.intervals:
        by_thread.setdefault(tid, []).append((s, e))
    assert len(by_thread) == 1, f"两个 run 应落在同一 thread，实际 {list(by_thread)}"
    ivals = sorted(by_thread[thread_id])
    for (s1, e1), (s2, _e2) in zip(ivals, ivals[1:], strict=False):
        assert e1 <= s2, f"同 thread 执行区间重叠: {(s1, e1)} vs {(s2, _e2)}"



async def _close_thread_lock_manager() -> None:
    """关闭共享 thread lock manager（释放其 Redis 连接），避免跨 event loop 复用。"""
    from runtime.thread_lock import (
        reset_thread_lock_manager_for_tests,
        shutdown_thread_lock_manager,
    )

    await shutdown_thread_lock_manager()
    reset_thread_lock_manager_for_tests()


def _ready(runtime):
    async def _p():
        return runtime

    return _p()


# ---------------------------------------------------------------------------
# Gate 4: 不同 thread 并发（耗时应接近单次 sleep）
# ---------------------------------------------------------------------------


def test_gate4_different_threads_run_concurrently(run_service, redis_url, unique, monkeypatch):
    """两个不同 thread 同时执行：总耗时应接近一次 sleep，而不是两次。"""
    import core.config as config
    from runtime.executor import execute_run
    from runtime.thread_lock import reset_thread_lock_manager_for_tests

    reset_thread_lock_manager_for_tests()
    monkeypatch.setattr(config, "AGENT_RUN_THREAD_LOCK_BACKEND", "redis")
    monkeypatch.setattr(config, "AGENT_RUN_THREAD_LOCK_TTL_SECONDS", 30.0)
    monkeypatch.setattr(config, "AGENT_RUN_LEASE_SECONDS", 60.0)

    sleep = 1.5
    t1, t2 = unique("T-par"), unique("T-par")
    r1 = run_service.create_run(query="p1", session_id=t1)
    r2 = run_service.create_run(query="p2", session_id=t2)

    runtime = _RecordingRuntime(sleep=sleep)

    async def noop_dispatch(run_id, countdown=None):
        return "task-stub"

    async def scenario():
        await asyncio.gather(
            execute_run(
                r1["id"],
                service=run_service,
                runtime_provider=lambda: _ready(runtime),
                dispatcher=noop_dispatch,
                worker_id="w1",
            ),
            execute_run(
                r2["id"],
                service=run_service,
                runtime_provider=lambda: _ready(runtime),
                dispatcher=noop_dispatch,
                worker_id="w2",
            ),
        )

    started = time.monotonic()
    asyncio.run(scenario())
    elapsed = time.monotonic() - started
    asyncio.run(_close_thread_lock_manager())

    assert run_service.require_run(r1["id"])["status"] == "SUCCEEDED"
    assert run_service.require_run(r2["id"])["status"] == "SUCCEEDED"
    assert runtime.max_overlap() == 2, "不同 thread 应真正同时执行（证明锁不是全局锁）"
    # 串行会是 ~3s；并发应接近 1.5s。留足余量仍能区分。
    assert elapsed < sleep * 1.8, (
        f"不同 thread 被串行化了：elapsed={elapsed:.2f}s, sleep={sleep}s"
    )


# ---------------------------------------------------------------------------
# Gate 5: lease 崩溃安全
# ---------------------------------------------------------------------------


def test_gate5_lease_owner_safety_and_crash_takeover(redis_url, unique):
    """非 owner 不能释放；owner 崩溃后 TTL 过期，新 owner 可接管（不死锁）。"""
    lock_a = _redis_lock(redis_url, "gate5:")
    lock_b = _redis_lock(redis_url, "gate5:")
    thread_id = unique("T-gate5")
    key = f"gate5:{thread_id}"

    async def scenario():
        # A 获得锁，owner token 随机且非布尔
        assert await lock_a.acquire(thread_id, "owner-A", 30.0) is True

        owner_token = await _raw_get(redis_url, key)
        assert owner_token == "owner-A"
        assert owner_token not in ("true", "1", "locked")

        # B 无法获得
        assert await lock_b.acquire(thread_id, "owner-B", 30.0) is False
        # B 无法释放 A 的锁（Lua compare-and-delete）
        assert await lock_b.release(thread_id, "owner-B") is False
        assert await _raw_get(redis_url, key) == "owner-A", "非 owner 不得删除锁"
        # B 无法续租
        assert await lock_b.refresh(thread_id, "owner-B", 30.0) is False

        # owner A 崩溃（不 release），锁必须靠 TTL 自动过期
        short = unique("T-gate5-short")
        assert await lock_a.acquire(short, "owner-C", 1.0) is True
        await asyncio.sleep(1.5)
        assert await lock_b.acquire(short, "owner-D", 30.0) is True, (
            "TTL 到期后必须能被接管，否则会永久死锁"
        )
        assert await _raw_get(redis_url, f"gate5:{short}") == "owner-D"

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(lock_a.close())
        asyncio.run(lock_b.close())


async def _raw_get(redis_url: str, key: str) -> str | None:
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url, decode_responses=True, socket_timeout=5)
    try:
        return await client.get(key)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Gate 8: transient 重试两次后成功
# ---------------------------------------------------------------------------


def test_gate8_transient_retry_then_success(run_service, unique, thread_lock_off):
    """attempt 1 FAIL / 2 FAIL / 3 SUCCESS -> SUCCEEDED，attempt == 3。"""
    from runtime.executor import execute_run

    session_id = unique("T-gate8")
    run = run_service.create_run(query="retry-me", session_id=session_id, max_attempts=3)

    dispatched: list[tuple[str, float | None]] = []

    async def dispatcher(run_id, countdown=None):
        dispatched.append((run_id, countdown))
        return "task-stub"

    # 同一个 runtime 实例跨三次 attempt：前两次抛超时，第三次成功。
    # 必须共享实例 —— 每次新建就等于每次都失败，测不出「第 3 次成功」。
    runtime = _RecordingRuntime(sleep=0, fail_times=2)

    statuses = []
    for i in range(3):
        statuses.append(
            asyncio.run(
                execute_run(
                    run["id"],
                    service=run_service,
                    runtime_provider=lambda: _ready(runtime),
                    dispatcher=dispatcher,
                    worker_id=f"w-{i}",
                )
            )
        )

    assert statuses[:2] == ["RETRYING", "RETRYING"], statuses
    assert statuses[2] == "SUCCEEDED", statuses

    row = run_service.require_run(run["id"])
    assert row["status"] == "SUCCEEDED"
    assert row["attempt"] == 3, f"attempt 应为 3，实际 {row['attempt']}"
    assert len(runtime.calls) == 3, f"图应被执行 3 次，实际 {len(runtime.calls)}"
    # 重试调度带退避
    assert dispatched and all(delay for _r, delay in dispatched[:2]), dispatched


def test_gate8_retry_metric_increments(run_service, thread_lock_off):
    from core import monitoring
    from runtime import metrics as run_metrics

    before = _counter("agent_run_retry_total")
    run_metrics.record_retry()
    assert _counter("agent_run_retry_total") == before + 1
    assert hasattr(monitoring, "agent_run_retry_total")


def _counter(name: str) -> float:
    try:
        from prometheus_client import REGISTRY
    except ImportError:  # pragma: no cover
        return 0.0
    total = 0.0
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name or sample.name == f"{name}_total":
                total += sample.value
    return total


# ---------------------------------------------------------------------------
# Gate 9: permanent 错误立即失败（不重试）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "exc"),
    [
        ("401", HttpStatusError(401)),
        ("403", HttpStatusError(403)),
        ("invalid_args", ValueError("bad argument")),
        ("business_validation", _permanent("SKU 不存在，无法下单")),
        ("unsupported_operation", NotImplementedError("该工具在当前环境不支持")),
    ],
)
def test_gate9_permanent_error_does_not_retry(
    run_service, unique, thread_lock_off, label, exc
):
    """401/403/参数非法/业务校验错误 -> 立即 FAILED，attempt == 1，不重试、不进 DLQ。"""
    from runtime.executor import execute_run

    run = run_service.create_run(query="perm", session_id=unique("T-gate9"), max_attempts=3)
    dispatched: list[str] = []

    async def dispatcher(run_id, countdown=None):
        dispatched.append(run_id)
        return "task-stub"

    runtime = _RecordingRuntime(sleep=0, fail_times=99, exc_factory=lambda: _clone(exc))

    status = asyncio.run(
        execute_run(
            run["id"],
            service=run_service,
            runtime_provider=lambda: _ready(runtime),
            dispatcher=dispatcher,
            worker_id="w-perm",
        )
    )

    row = run_service.require_run(run["id"])
    assert status == "FAILED", f"{label}: {status} / {row}"
    assert row["status"] == "FAILED"
    assert row["attempt"] == 1, f"{label} 不应消耗重试次数，实际 attempt={row['attempt']}"
    assert dispatched == [], f"{label} 不应触发重试调度"
    assert run_service.get_dead_letter(run["id"]) is None, f"{label} 不应进 DLQ"
    assert row["error_type"] == "permanent", f"{label}: {row['error_type']}"


# ---------------------------------------------------------------------------
# Gate 10: DLQ + 重放
# ---------------------------------------------------------------------------


def test_gate10_dead_letter_records_evidence_and_replays(run_service, unique, thread_lock_off):
    """retry 用尽 -> DEAD_LETTER，证据齐全；``requeue_dead_letter`` 可重新投递且保留历史。"""
    from runtime.executor import execute_run

    session_id = unique("T-gate10")
    run = run_service.create_run(query="dlq-me", session_id=session_id, max_attempts=2)

    async def dispatcher(run_id, countdown=None):
        return "task-stub"

    statuses = []
    for i in range(2):
        rt = _RecordingRuntime(sleep=0, fail_times=1)
        statuses.append(
            asyncio.run(
                execute_run(
                    run["id"],
                    service=run_service,
                    runtime_provider=(lambda r=rt: _ready(r)),
                    dispatcher=dispatcher,
                    worker_id=f"w-dlq-{i}",
                )
            )
        )

    assert statuses == ["RETRYING", "DEAD_LETTER"], statuses
    row = run_service.require_run(run["id"])
    assert row["status"] == "DEAD_LETTER"
    assert row["attempt"] == 2

    dlq = run_service.get_dead_letter(run["id"])
    assert dlq is not None, "必须存在 DLQ 记录"
    assert dlq["run_id"] == run["id"]
    assert dlq["thread_id"] == session_id
    assert dlq["attempt_count"] == 2
    assert dlq["error_type"] == "timeout"
    assert dlq["error_code"]
    assert dlq["entered_at"] is not None

    # 重放：状态回 QUEUED，attempt 归零
    replayed = run_service.requeue_dead_letter(run["id"])
    assert replayed["status"] == "QUEUED"
    assert replayed["attempt"] == 0
    assert replayed["finished_at"] is None

    # 原始失败历史必须仍在（不可变证据）
    dlq_after = run_service.get_dead_letter(run["id"])
    assert dlq_after is not None
    assert dlq_after["attempt_count"] == 2
    assert dlq_after["error_type"] == "timeout"
    assert dlq_after["entered_at"] == dlq["entered_at"]

    # 非 DEAD_LETTER 不可重放
    from runtime.statuses import InvalidRunTransition

    with pytest.raises(InvalidRunTransition):
        run_service.requeue_dead_letter(run["id"])


def test_gate10_replay_keeps_run_id_so_tool_idempotency_survives(
    run_service, unique, thread_lock_off
):
    """重放必须复用 run_id —— 否则工具幂等键会变，已成功的副作用会被再执行一次。"""
    from runtime.side_effects import build_tool_idempotency_key

    run = run_service.create_run(query="dlq-key", session_id=unique("T-gate10k"))
    before_key = build_tool_idempotency_key(run["id"], "call-1")
    run_service.mark_running(run["id"], worker_id="w", lease_seconds=60)
    run_service.mark_dead_letter(run["id"], error_code="X", error_message="y", error_type="timeout")
    replayed = run_service.requeue_dead_letter(run["id"])
    assert replayed["id"] == run["id"]
    assert build_tool_idempotency_key(replayed["id"], "call-1") == before_key


# ---------------------------------------------------------------------------
# Gate 12: run 查询字段 + 不泄露 secret
# ---------------------------------------------------------------------------


def test_gate12_cancel_run_transitions_to_cancelled(run_service, unique, thread_lock_off):
    """协作式取消：QUEUED -> CANCELLED；worker 不会再执行它。"""
    run = run_service.create_run(query="cancel-me", session_id=unique("T-cancel"))
    assert run["status"] == "QUEUED"

    cancelled = run_service.cancel_run(run["id"])
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["finished_at"] is not None

    # 终态不可再迁移
    again = run_service.cancel_run(run["id"])
    assert again["status"] == "CANCELLED"

    from runtime.executor import execute_run

    runtime = _RecordingRuntime(sleep=0)
    status = asyncio.run(
        execute_run(
            run["id"],
            service=run_service,
            runtime_provider=lambda: _ready(runtime),
            dispatcher=None,
            worker_id="w-cancel",
        )
    )
    assert status == "CANCELLED"
    assert runtime.calls == [], "已取消的 run 不得执行图"


def test_gate12_run_query_fields_present(run_service, unique, thread_lock_off):
    """run 查询必须包含验收要求的全部字段。"""
    from runtime.executor import execute_run

    run = run_service.create_run(query="fields", session_id=unique("T-fields"))

    async def dispatcher(run_id, countdown=None):
        return "task-stub"

    runtime = _RecordingRuntime(sleep=0)
    asyncio.run(
        execute_run(
            run["id"],
            service=run_service,
            runtime_provider=lambda: _ready(runtime),
            dispatcher=dispatcher,
            worker_id="w-fields",
        )
    )

    row = run_service.require_run(run["id"])
    for field in (
        "id",
        "thread_id",
        "status",
        "attempt",
        "created_at",
        "started_at",
        "finished_at",
        "error_code",
        "worker_id",
    ):
        assert field in row, f"run 查询缺少字段 {field}"

    blob = repr(row)
    for secret_marker in ("JWT_SECRET", "API_KEY", "password", "Bearer ", "sk-"):
        assert secret_marker not in blob, f"run 查询结果疑似泄露 {secret_marker}"


def test_gate12_pending_status_is_supported(run_service, unique):
    """PENDING -> QUEUED 两阶段创建路径可用。"""
    from runtime.statuses import RunStatus

    run = run_service.create_run(
        query="two-phase",
        session_id=unique("T-pending"),
        status=RunStatus.PENDING.value,
    )
    assert run["status"] == "PENDING"
    assert run["queued_at"] is None

    queued = run_service.mark_queued(run["id"])
    assert queued["status"] == "QUEUED"
    assert queued["queued_at"] is not None
