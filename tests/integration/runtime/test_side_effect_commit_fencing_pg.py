"""P0 复现：side-effect ledger 的结果提交（``mark_succeeded`` / ``mark_failed``）
必须由**数据库**原子地按「所有权 + 状态 + 租约」判定，而不是「先 SELECT 判断、
再按主键 UPDATE」。

历史缺陷（本文件的复现对象）
---------------------------
``mark_succeeded`` / ``mark_failed`` 过去是::

    SELECT ... WHERE tool_name=? AND operation_key=? [AND claim_owner=?]   # 判断
    row.status = ...                                                       # 改 ORM 对象
    session.commit()                                                       # -> UPDATE ... WHERE id = :pk

``ToolSideEffect`` 没有 ``version_id_col``，所以 SQLAlchemy 真实提交的 UPDATE
**只按主键**（真机 SQLAlchemy 1.4 实测，SQL 里只有 ``WHERE tool_side_effects.id = :pk``）。
归属判断发生在**另一条语句**里，两条语句之间存在真实窗口：

1. Worker A SELECT 读到 ``claim_owner = A``（通过）；
2. A 在发出 UPDATE 之前被暂停（SIGSTOP / GC / 调度）；
3. A 的租约过期，Worker B 原子接管，``claim_owner = B`` 且**租约是新的**；
4. A 恢复，按主键提交，把 B 的 ``PENDING`` 覆盖成 ``SUCCEEDED``/``FAILED``。

后果：B 的外部写操作已经发起，A 的结果却顶掉了 ledger 的记录；反过来 B 的迟到
提交又会顶掉 A 的结果——「已成功的操作被迟到的异常改写」。

本文件用**真实 PostgreSQL + 多连接 + 受控并发屏障**复现（``before_cursor_execute``
在真正发出 UPDATE 前把 A 按住，而不是靠读源码或 ``sleep`` 赌时序），并在修复后
断言同批场景全通过。

对照锚点：同模块的 ``renew_claim`` 一直是正确的单条条件 UPDATE；``claim()`` 的
接管写在 ``SELECT ... FOR UPDATE`` 行锁内，也是原子的。缺的只有结果提交这一条。

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_side_effect_commit_fencing_pg.py -q
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid

import pytest
from sqlalchemy import event, text

from runtime.side_effects import SideEffectStore, execute_idempotent_operation

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
requires_infra = pytest.mark.skipif(
    not (
        os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip() and os.getenv("TEST_REDIS_URL", "").strip()
    ),
    reason=INFRA_REASON,
)

_FRESH_TTL = 60.0
#: 负 TTL 让认领**立刻**过期（与既有 PG 用例一致），用来构造接管场景。
_EXPIRED_TTL = -1.0
_BARRIER_TIMEOUT = 60.0

_ROW_COLUMNS = (
    "status, claim_owner, result_reference, error_type, "
    "claim_expires_at IS NULL AS lease_cleared, request_fingerprint"
)


def _session_factory(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def store(pg_engine):
    return SideEffectStore(session_factory=_session_factory(pg_engine))


@pytest.fixture
def ledger_keys(pg_engine):
    """记录并清理本用例写入的 ledger 行（共享测试库）。"""
    recorded: list[str] = []
    yield recorded
    if recorded:
        with pg_engine.begin() as conn:
            for key in recorded:
                conn.execute(
                    text("DELETE FROM tool_side_effects WHERE operation_key = :k"),
                    {"k": key},
                )


@pytest.fixture
def new_op(ledger_keys):
    """为一次逻辑操作分配唯一 (tool, key)，并登记清理。"""

    def _make(tool: str = "staging_refund") -> tuple[str, str]:
        key = f"commit-fence-{uuid.uuid4().hex[:12]}"
        ledger_keys.append(key)
        return tool, key

    return _make


def read_row(pg_engine, tool: str, key: str) -> dict:
    """绕过 ORM 直接读数据库当前状态（断言必须落在真实持久化值上）。"""
    with pg_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    f"SELECT {_ROW_COLUMNS} FROM tool_side_effects "  # noqa: S608 - 常量拼接
                    "WHERE tool_name = :t AND operation_key = :k"
                ),
                {"t": tool, "k": key},
            )
            .mappings()
            .one()
        )
    return dict(row)


class UpdateBarrier:
    """受控并发屏障：把指定线程的 UPDATE 按在**真正发出之前**。

    为什么必须拦在 ``before_cursor_execute``
    ---------------------------------------
    缺陷发生在「判断的 SELECT」与「提交的 UPDATE」之间。要确定性地命中这个窗口，
    只能拦在 ORM flush 真正要发 UPDATE 的那一刻——这正是真实驱动会执行的真实
    SQL，而不是用 ``sleep`` 去赌时序或只读源码推断。
    """

    def __init__(self, engine) -> None:
        self._engine = engine
        self._local = threading.local()
        self.reached = threading.Event()
        self.release = threading.Event()

    def arm(self) -> None:
        self._local.armed = True

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if not getattr(self._local, "armed", False):
            return
        if not statement.lstrip().upper().startswith("UPDATE TOOL_SIDE_EFFECTS"):
            return
        self._local.armed = False
        self.reached.set()
        if not self.release.wait(timeout=_BARRIER_TIMEOUT):
            raise AssertionError("并发屏障超时：被拦住的线程没有收到释放信号")

    def __enter__(self) -> UpdateBarrier:
        event.listen(self._engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self._engine, "before_cursor_execute", self._on_execute)


class _CommitCounter:
    """跨 session 计数的 commit 门闩（第 N 次起抛错）。

    必须**共享**：``SideEffectStore`` 每次操作都开新 session，per-session 计数器
    永远停在第 1 次，根本触发不到结果提交。
    """

    def __init__(self, *, fail_from: int, error: BaseException) -> None:
        self.fail_from = fail_from
        self.error = error
        self.count = 0

    def commit(self, real_commit):
        self.count += 1
        if self.count >= self.fail_from:
            raise self.error
        return real_commit()


class _CommitFailsAfterClaim:
    """真实 session，只替换 ``commit``；其余行为（SELECT / flush）都是真的。"""

    def __init__(self, real, counter: _CommitCounter) -> None:
        self._real = real
        self._counter = counter

    def __getattr__(self, name):
        return getattr(self._real, name)

    def commit(self):
        return self._counter.commit(self._real.commit)


def run_in_thread(fn) -> tuple[threading.Thread, dict]:
    """在独立线程执行阻塞调用并回收结果/异常（对应独立 DB 连接）。"""
    box: dict = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 测试需要把异常带回主线程
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, box


def _join(thread: threading.Thread, box: dict) -> None:
    thread.join(timeout=_BARRIER_TIMEOUT)
    assert not thread.is_alive(), "线程未在超时内结束"
    assert "error" not in box, box["error"]


# ---------------------------------------------------------------------------
# 场景 1：A 读到 owner=A -> B 接管（**租约是新的**）-> A 迟到提交 SUCCEEDED
# ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.timeout(120)
def test_old_owner_cannot_overwrite_new_owner_with_succeeded(pg_engine, store, new_op):
    tool, key = new_op()

    claim_a = store.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-a",
        thread_id="T-fence",
        fingerprint="fp-amount-100",
        claim_ttl_seconds=_EXPIRED_TTL,
        owner="owner-A",
    )
    assert claim_a.state == "execute"

    def _late_commit_by_a() -> bool:
        barrier.arm()
        return store.mark_succeeded(tool, key, "refund-by-A", owner="owner-A")

    # A 已经读到 claim_owner=A，正准备提交 —— 按住它的 UPDATE。
    with UpdateBarrier(pg_engine) as barrier:
        thread_a, box_a = run_in_thread(_late_commit_by_a)
        assert barrier.reached.wait(timeout=_BARRIER_TIMEOUT), "A 未到达 UPDATE 提交点"

        # A 的租约已过期，B 原子接管，且拿到的是**未过期的新租约**。
        claim_b = store.claim(
            tool_name=tool,
            operation_key=key,
            run_id="run-b",
            thread_id="T-fence",
            fingerprint="fp-amount-100",
            claim_ttl_seconds=_FRESH_TTL,
            owner="owner-B",
        )
        assert claim_b.state == "execute", claim_b.state
        taken_over = read_row(pg_engine, tool, key)
        assert taken_over["claim_owner"] == "owner-B"
        assert taken_over["status"] == "PENDING"
        assert taken_over["lease_cleared"] is False, "B 的租约必须是未过期的"

        barrier.release.set()
        _join(thread_a, box_a)

    # 关键：B 的租约**仍未过期**，因此「拒绝 A」只能来自**所有权**谓词，
    # 不能是「租约过期」谓词顺带挡住的。
    assert box_a["value"] is False, "旧 owner 的迟到提交被接受了"
    final = read_row(pg_engine, tool, key)
    assert final["claim_owner"] == "owner-B"
    assert final["status"] == "PENDING", "旧 owner 覆盖了接管者的 PENDING"
    assert final["result_reference"] is None

    # 接管者仍然可以把结果提交上去。
    assert store.mark_succeeded(tool, key, "refund-by-B", owner="owner-B") is True
    assert read_row(pg_engine, tool, key)["result_reference"] == "refund-by-B"


# ---------------------------------------------------------------------------
# 场景 2：A 读到 owner=A -> B 接管并已 SUCCEEDED -> A 迟到提交 FAILED
# ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.timeout(120)
def test_old_owner_cannot_turn_new_owner_success_into_failed(pg_engine, store, new_op):
    tool, key = new_op()
    claim_a = store.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-a",
        thread_id="T-fence",
        fingerprint="fp",
        claim_ttl_seconds=_EXPIRED_TTL,
        owner="owner-A",
    )
    assert claim_a.state == "execute"

    claim_b = store.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-b",
        thread_id="T-fence",
        fingerprint="fp",
        claim_ttl_seconds=_FRESH_TTL,
        owner="owner-B",
    )
    assert claim_b.state == "execute"
    assert store.mark_succeeded(tool, key, "refund-ok-B", owner="owner-B") is True

    # A 在被 SIGSTOP 之前已经通过了归属判断，现在恢复并提交失败。
    assert (
        store.mark_failed(
            tool, key, error_type="TimeoutError", error_message="late from A", owner="owner-A"
        )
        is False
    )

    final = read_row(pg_engine, tool, key)
    assert final["status"] == "SUCCEEDED", "已成功的操作被迟到异常改写成 FAILED"
    assert final["result_reference"] == "refund-ok-B"
    assert final["error_type"] is None


# ---------------------------------------------------------------------------
# 场景 3：A 与 B 同时提交结果 —— 恰好一个赢家
# ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.timeout(180)
def test_concurrent_commits_have_exactly_one_winner(pg_engine, store, new_op):
    tool, key = new_op()
    claim_a = store.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-a",
        thread_id="T-fence",
        fingerprint="fp",
        claim_ttl_seconds=_EXPIRED_TTL,
        owner="owner-A",
    )
    assert claim_a.state == "execute"
    claim_b = store.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-b",
        thread_id="T-fence",
        fingerprint="fp",
        claim_ttl_seconds=_FRESH_TTL,
        owner="owner-B",
    )
    assert claim_b.state == "execute"

    start = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

    def _commit(owner: str, value: str) -> bool:
        start.wait()
        return store.mark_succeeded(tool, key, value, owner=owner)

    thread_a, box_a = run_in_thread(lambda: _commit("owner-A", "by-A"))
    thread_b, box_b = run_in_thread(lambda: _commit("owner-B", "by-B"))
    _join(thread_a, box_a)
    _join(thread_b, box_b)

    outcomes = [box_a["value"], box_b["value"]]
    assert outcomes.count(True) == 1, f"必须恰好一个赢家，实际 {outcomes}"

    final = read_row(pg_engine, tool, key)
    assert final["status"] == "SUCCEEDED"
    assert final["claim_owner"] == "owner-B"
    assert final["result_reference"] == "by-B", "非当前 owner 的结果落库了"


# ---------------------------------------------------------------------------
# 场景 4：租约已过期但**尚未被接管** —— 迟到提交不能复活执行权
# ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.timeout(120)
def test_expired_unclaimed_lease_cannot_be_resurrected_by_late_commit(pg_engine, store, new_op):
    tool, key = new_op()
    claim_a = store.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-a",
        thread_id="T-fence",
        fingerprint="fp",
        claim_ttl_seconds=_EXPIRED_TTL,
        owner="owner-A",
    )
    assert claim_a.state == "execute"
    assert read_row(pg_engine, tool, key)["claim_owner"] == "owner-A"

    # 租约过期，但没有任何人来接管：这一行仍属于 A，只是租约已经失效。
    assert store.mark_succeeded(tool, key, "resurrected", owner="owner-A") is False
    assert read_row(pg_engine, tool, key)["status"] == "PENDING"
    assert (
        store.mark_failed(tool, key, error_type="X", error_message="late", owner="owner-A") is False
    )
    assert read_row(pg_engine, tool, key)["status"] == "PENDING"


# ---------------------------------------------------------------------------
# 场景 5：续租正常期间提交成功（不许误伤正常路径）
# ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.timeout(120)
def test_renewed_claim_commits_successfully(pg_engine, store, new_op):
    tool, key = new_op()
    claim = store.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-a",
        thread_id="T-fence",
        fingerprint="fp",
        claim_ttl_seconds=_FRESH_TTL,
        owner="owner-A",
    )
    assert claim.state == "execute"
    assert store.renew_claim(tool, key, owner="owner-A", claim_ttl_seconds=_FRESH_TTL) is True
    assert store.mark_succeeded(tool, key, "ok-renewed", owner="owner-A") is True

    final = read_row(pg_engine, tool, key)
    assert final["status"] == "SUCCEEDED"
    assert final["result_reference"] == "ok-renewed"
    assert final["lease_cleared"] is True

    # 失败路径同样在续租下可用。
    tool2, key2 = new_op()
    claim2 = store.claim(
        tool_name=tool2,
        operation_key=key2,
        run_id="run-a",
        thread_id="T-fence",
        fingerprint="fp",
        claim_ttl_seconds=_FRESH_TTL,
        owner="owner-A",
    )
    assert claim2.state == "execute"
    assert (
        store.mark_failed(tool2, key2, error_type="Boom", error_message="down", owner="owner-A")
        is True
    )
    assert read_row(pg_engine, tool2, key2)["status"] == "FAILED"


# ---------------------------------------------------------------------------
# 场景 6：已 SUCCEEDED 的操作收到迟到的失败回调
# ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.timeout(120)
def test_late_failure_callback_cannot_rewrite_a_succeeded_operation(pg_engine, store, new_op):
    tool, key = new_op()
    claim = store.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-a",
        thread_id="T-fence",
        fingerprint="fp",
        claim_ttl_seconds=_FRESH_TTL,
        owner="owner-A",
    )
    assert claim.state == "execute"
    assert store.mark_succeeded(tool, key, "done", owner="owner-A") is True

    assert (
        store.mark_failed(
            tool, key, error_type="CancelledError", error_message="worker died", owner="owner-A"
        )
        is False
    )
    assert store.mark_succeeded(tool, key, "overwritten", owner="owner-A") is False
    final = read_row(pg_engine, tool, key)
    assert final["status"] == "SUCCEEDED"
    assert final["result_reference"] == "done"
    assert final["error_type"] is None


# ---------------------------------------------------------------------------
# 场景 7：数据库不可用导致结果提交失败 -> 调用方不得报告外部操作成功
# ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.timeout(120)
def test_ledger_commit_failure_never_reports_external_success(pg_engine, new_op):
    from sqlalchemy.exc import OperationalError

    tool, key = new_op()

    commits = _CommitCounter(
        fail_from=2,
        error=OperationalError("COMMIT", None, Exception("server closed the connection")),
    )

    real_factory = _session_factory(pg_engine)
    flaky_store = SideEffectStore(
        session_factory=lambda: _CommitFailsAfterClaim(real_factory(), commits)
    )

    executed: list[dict] = []

    def _external_write(arguments: dict) -> str:
        executed.append(dict(arguments))
        return "erp-refund-accepted"

    # 关键：外部写**已经发生**（ledger 提交阶段才失败）——此时上报成功是不可接受的。
    with pytest.raises(OperationalError):
        asyncio.run(
            execute_idempotent_operation(
                tool_name=tool,
                operation_key=key,
                run_id="run-a",
                thread_id="T-fence",
                arguments={"amount": 100},
                operation=lambda: _external_write({"amount": 100}),
                store=flaky_store,
                claim_ttl_seconds=_FRESH_TTL,
                min_claim_ttl_seconds=1.0,
            )
        )

    assert executed == [{"amount": 100}], "外部副作用未发生，用例没测到提交阶段"
    # ledger 没写成终态 -> 后续重投会经过 claim 重试，而不是复用不存在的成功结果。
    assert read_row(pg_engine, tool, key)["status"] == "PENDING"


@requires_infra
@pytest.mark.timeout(120)
def test_ledger_failure_does_not_mask_the_original_tool_exception(pg_engine, new_op):
    """失败路径上 ledger 自身故障不得**替换**真实业务异常。

    历史行为：``mark_failed`` 的 commit 抛出会顶掉工具原本的异常，运维看到的是
    一个基础设施错误，真实失败原因（业务侧）反而丢失。
    """

    class _ToolBoom(RuntimeError):
        pass

    commits = _CommitCounter(fail_from=2, error=RuntimeError("ledger commit exploded"))

    tool, key = new_op()
    real_factory = _session_factory(pg_engine)
    flaky_store = SideEffectStore(
        session_factory=lambda: _CommitFailsAfterClaim(real_factory(), commits)
    )

    def _explode() -> str:
        raise _ToolBoom("ERP returned 500")

    with pytest.raises(_ToolBoom):
        asyncio.run(
            execute_idempotent_operation(
                tool_name=tool,
                operation_key=key,
                run_id="run-a",
                thread_id="T-fence",
                arguments={"amount": 100},
                operation=_explode,
                store=flaky_store,
                claim_ttl_seconds=_FRESH_TTL,
                min_claim_ttl_seconds=1.0,
            )
        )


# ---------------------------------------------------------------------------
# 场景 8：两个不同的 logical operation 不得错误合并
# ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.timeout(120)
def test_distinct_logical_operations_never_merge(pg_engine, store, new_op):
    tool_a, key_a = new_op("staging_refund")
    tool_b, key_b = new_op("staging_refund")
    tool_other = "staging_order_update"

    planned = [
        (tool_a, key_a, "owner-1"),
        (tool_b, key_b, "owner-2"),
        # 复用与第一个**相同**的 operation_key，但工具不同：不得合并。
        (tool_other, key_a, "owner-3"),
    ]

    for tool, key, owner in planned:
        claim = store.claim(
            tool_name=tool,
            operation_key=key,
            run_id="run-a",
            thread_id="T-fence",
            fingerprint="fp",
            claim_ttl_seconds=_FRESH_TTL,
            owner=owner,
        )
        assert claim.state == "execute", (tool, key, claim.state)

    for tool, key, owner in planned:
        assert store.mark_succeeded(tool, key, f"result-of-{owner}", owner=owner) is True

    for tool, key, owner in planned:
        assert read_row(pg_engine, tool, key)["result_reference"] == f"result-of-{owner}"

    with pg_engine.connect() as conn:
        total = conn.execute(
            text("SELECT count(*) FROM tool_side_effects WHERE operation_key = :k"),
            {"k": key_a},
        ).scalar_one()
    # 相同 operation_key、不同 tool_name 必须是两行独立 ledger。
    assert total == 2, f"不同工具的同名操作被合并成了 {total} 行"
    assert key_b not in (key_a,)


# ---------------------------------------------------------------------------
# 契约：真实提交的 UPDATE 必须带所有权 + 状态 + 租约谓词
# ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.timeout(120)
def test_commit_update_sql_carries_ownership_status_and_lease_predicates(pg_engine, store, new_op):
    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.lstrip().upper().startswith("UPDATE TOOL_SIDE_EFFECTS"):
            captured.append(" ".join(statement.split()))

    tool_ok, key_ok = new_op("staging_refund")
    tool_fail, key_fail = new_op("staging_order_update")

    event.listen(pg_engine, "before_cursor_execute", _capture)
    try:
        claim = store.claim(
            tool_name=tool_ok,
            operation_key=key_ok,
            run_id="run-a",
            thread_id="T-fence",
            fingerprint="fp",
            claim_ttl_seconds=_FRESH_TTL,
            owner="owner-A",
        )
        assert claim.state == "execute"
        assert store.mark_succeeded(tool_ok, key_ok, "ok", owner="owner-A") is True

        claim2 = store.claim(
            tool_name=tool_fail,
            operation_key=key_fail,
            run_id="run-a",
            thread_id="T-fence",
            fingerprint="fp",
            claim_ttl_seconds=_FRESH_TTL,
            owner="owner-A",
        )
        assert claim2.state == "execute"
        assert (
            store.mark_failed(
                tool_fail, key_fail, error_type="E", error_message="m", owner="owner-A"
            )
            is True
        )
    finally:
        event.remove(pg_engine, "before_cursor_execute", _capture)

    assert len(captured) == 2, captured
    for sql in captured:
        lowered = sql.lower()
        assert "claim_owner" in lowered, f"UPDATE 缺少所有权谓词: {sql}"
        assert "status" in lowered, f"UPDATE 缺少状态谓词: {sql}"
        assert "claim_expires_at" in lowered, f"UPDATE 缺少租约谓词: {sql}"
        # 只按主键更新就是缺陷本身：显式禁止。
        assert "where tool_side_effects.id = " not in lowered, f"结果提交退化成只按主键更新: {sql}"


# ---------------------------------------------------------------------------
# 契约：owner 缺失不得悄悄绕过归属校验
# ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.timeout(120)
def test_missing_owner_is_rejected_instead_of_bypassing_the_check(pg_engine, store, new_op):
    tool, key = new_op()
    claim = store.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-a",
        thread_id="T-fence",
        fingerprint="fp",
        claim_ttl_seconds=_FRESH_TTL,
        owner="owner-A",
    )
    assert claim.state == "execute"

    with pytest.raises(Exception) as excinfo:  # noqa: B017 - 断言消息内容
        store.mark_succeeded(tool, key, "no-owner", owner=None)
    assert "owner" in str(excinfo.value).lower(), str(excinfo.value)

    with pytest.raises(Exception) as excinfo:  # noqa: B017 - 断言消息内容
        store.mark_failed(tool, key, error_type="E", error_message="m", owner=None)
    assert "owner" in str(excinfo.value).lower(), str(excinfo.value)

    assert (
        read_row(pg_engine, tool, key)["status"] == "PENDING"
    ), "缺少 owner 的调用仍然写入了 ledger"
