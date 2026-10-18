"""A1 复现：节点重放产生新 ``tool_call_id`` -> ledger 失效 -> 副作用重复执行。

缺陷
----
非 HITL 写操作的 ``operation_key`` 是 ``run_id + ":" + tool_call_id``，而
``tool_call_id`` 来自 **LLM 生成的 tool call id**（``agents/base_agent.py``:
``tool_call_id = p.get("id")``）。节点在 checkpoint 未提交前崩溃后被重放时，LLM 被
重新调用，可以产生**不同的** tool call id —— 于是同一笔逻辑操作拿到新的
``operation_key``，``tool_side_effects`` 的唯一约束看不到任何重复，副作用**执行两次**。

本文件用确定性 fault injection 证明这一点，不依赖 Mock-LLM 的自然语言行为：

- ``operation_key`` 的唯一输入（``run_id`` / ``tool_call_id`` / ``thread_id``）；
- 重放时 ``tool_call_id`` 改变；
- 断言 ledger 是否把两次认领当成两个独立操作。

然后验证修复后的契约（见 ``test_replay_stable_key_*``）：
跨重放**同一逻辑调用**必须收敛到同一个 ``operation_key``；而两次**合法但不同的**
业务操作不得被合并。

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_side_effect_replay_identity_pg.py -q
"""

from __future__ import annotations

import os
import uuid

import pytest

from runtime.side_effects import SideEffectStore, execute_idempotent_operation

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
requires_infra = pytest.mark.skipif(
    not (
        os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip() and os.getenv("TEST_REDIS_URL", "").strip()
    ),
    reason=INFRA_REASON,
)


@pytest.fixture
def store(pg_engine):
    from sqlalchemy.orm import sessionmaker

    return SideEffectStore(session_factory=sessionmaker(bind=pg_engine, expire_on_commit=False))


@pytest.fixture
def cleanup_side_effects(pg_engine):
    """删除本用例创建的 ledger 行。

    ``tool_side_effects`` 没有「扫描窗口」，但共享测试库里既有行会让唯一约束在
    断言中互相干扰；按本次运行的 operation_key 前缀精确删除。
    """
    from sqlalchemy import text

    recorded: list[str] = []
    yield recorded
    if not recorded:
        return
    with pg_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM tool_side_effects WHERE operation_key = ANY(:keys)"),
            {"keys": recorded},
        )


@pytest.fixture(autouse=True)
def _unique_prefix(cleanup_side_effects):
    """每个用例用独立前缀，行不会互相干扰。"""
    global prefix
    prefix = f"a1-{uuid.uuid4().hex[:8]}"
    yield


class _ExternalSystem:
    """确定性的「外部系统」：记录每次真实写调用，不做任何去重。

    这正是「下游没有幂等接口」的形状：本地 ledger 是唯一的防线，它错了就会
    真的写两次。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def refund(self, arguments: dict) -> str:
        self.calls.append(dict(arguments))
        return f"ext-ok:{len(self.calls)}"

    @property
    def executions(self) -> int:
        return len(self.calls)


def _op_key(run_id: str, tool_call_id: str) -> str:
    """复现生产路径的键构造（``tools/tool_registry.py::_idempotent_operation``）。"""
    from runtime.side_effects import build_tool_idempotency_key

    return build_tool_idempotency_key(run_id, tool_call_id)


# ---------------------------------------------------------------------------
# 缺陷复现
# ---------------------------------------------------------------------------


@requires_infra
@requires_infra
async def test_tool_call_id_key_is_unstable_across_replay(store):
    """记录**为什么** ``run_id:tool_call_id`` 不能用于非 HITL 写操作。

    这是 A1 的缺陷本体。它**不会**随修复消失——``tool_call_id`` 由 LLM 生成，节点
    重放时可变。因此本用例断言的是一个永久成立的事实（键会变），而不是断言
    「缺陷存在」。

    修复前的实测结论：同一逻辑写操作被执行两次（外部系统 2 次），ledger 里留下两行
    互不相识的记录。证据见 PR #11 与本文件 docstring。
    """
    run_id = f"{prefix}-run"
    thread_id = f"{prefix}-thread"

    first_key = _op_key(run_id, "call_A")
    replay_key = _op_key(run_id, "call_B")
    assert first_key != replay_key, "tool_call_id 变了 -> 幂等键必然变 -> ledger 失效"

    external = _ExternalSystem()
    await execute_idempotent_operation(
        tool_name="staging_refund",
        operation_key=first_key,
        run_id=run_id,
        thread_id=thread_id,
        arguments={"order_id": "ORD-1", "amount": 100},
        operation=lambda: external.refund({"order_id": "ORD-1", "amount": 100}),
        store=store,
    )
    await execute_idempotent_operation(
        tool_name="staging_refund",
        operation_key=replay_key,
        run_id=run_id,
        thread_id=thread_id,
        arguments={"order_id": "ORD-1", "amount": 100},
        operation=lambda: external.refund({"order_id": "ORD-1", "amount": 100}),
        store=store,
    )
    assert external.executions == 2, (
        "两个不同的键 = 两笔独立操作，各自执行一次。这正是重复写的来源，"
        "也是改用重放稳定键的动机。"
    )
    assert (
        store.get("staging_refund", first_key)["operation_key"]
        != store.get("staging_refund", replay_key)["operation_key"]
    )


# ---------------------------------------------------------------------------
# 修复后的契约：跨重放稳定的逻辑操作身份
# ---------------------------------------------------------------------------


@requires_infra
async def test_replay_stable_key_dedupes_same_logical_operation(store):
    """修复后：同一逻辑调用在重放时拿到**同一个** operation_key，副作用只发生一次。"""
    from runtime.side_effects import build_replay_stable_operation_key

    run_id = f"{prefix}-run3"
    thread_id = f"{prefix}-thread3"
    external = _ExternalSystem()

    def key_for(node: str, agent: str, ordinal: int) -> str:
        return build_replay_stable_operation_key(
            run_id=run_id,
            node_id=node,
            agent=agent,
            tool_name="staging_refund",
            ordinal=ordinal,
        )

    k1 = key_for("tools_node", "refund_agent", 0)
    # 重放：tool_call_id 变了，但逻辑身份（run/node/agent/tool/ordinal）没变
    k2 = key_for("tools_node", "refund_agent", 0)
    assert k1 == k2, "同一逻辑调用必须得到同一个键（与 tool_call_id 无关）"

    await execute_idempotent_operation(
        tool_name="staging_refund",
        operation_key=k1,
        run_id=run_id,
        thread_id=thread_id,
        arguments={"order_id": "ORD-3", "amount": 70},
        operation=lambda: external.refund({"order_id": "ORD-3", "amount": 70}),
        store=store,
    )
    # 重放
    await execute_idempotent_operation(
        tool_name="staging_refund",
        operation_key=k2,
        run_id=run_id,
        thread_id=thread_id,
        arguments={"order_id": "ORD-3", "amount": 70},
        operation=lambda: external.refund({"order_id": "ORD-3", "amount": 70}),
        store=store,
    )
    assert external.executions == 1, "重放必须被 ledger 去重"


@requires_infra
async def test_replay_stable_key_separates_distinct_logical_operations(store):
    """修复后：不得把两次**合法但不同**的业务操作合并。

    四种区分维度逐一验证——这是「不能只按 tool_name + arguments 全局去重」的直接
    对应测试。
    """
    from runtime.side_effects import build_replay_stable_operation_key

    base = dict(tool_name="staging_refund")
    run_id = f"{prefix}-run4"

    a = build_replay_stable_operation_key(
        run_id=run_id, node_id="n1", agent="a1", ordinal=0, **base
    )
    # 不同 ordinal = 同一节点内不同的逻辑调用
    b = build_replay_stable_operation_key(
        run_id=run_id, node_id="n1", agent="a1", ordinal=1, **base
    )
    # 不同 node
    c = build_replay_stable_operation_key(
        run_id=run_id, node_id="n2", agent="a1", ordinal=0, **base
    )
    # 不同 agent
    d = build_replay_stable_operation_key(
        run_id=run_id, node_id="n1", agent="a2", ordinal=0, **base
    )
    # 不同 run（= 用户主动再次发起）
    e = build_replay_stable_operation_key(
        run_id=f"{run_id}-other", node_id="n1", agent="a1", ordinal=0, **base
    )

    keys = [a, b, c, d, e]
    assert len(set(keys)) == len(keys), f"不同逻辑操作不得共用键: {keys}"

    # 并且：参数不同时，同一个键会被指纹冲突挡住（fail-closed，不执行）
    external = _ExternalSystem()
    await execute_idempotent_operation(
        tool_name="staging_refund",
        operation_key=a,
        run_id=run_id,
        thread_id=f"{prefix}-t",
        arguments={"order_id": "ORD-4", "amount": 10},
        operation=lambda: external.refund({"order_id": "ORD-4", "amount": 10}),
        store=store,
    )
    from runtime.errors import PermanentError

    with pytest.raises(PermanentError):
        await execute_idempotent_operation(
            tool_name="staging_refund",
            operation_key=a,
            run_id=run_id,
            thread_id=f"{prefix}-t",
            # 同一逻辑槽位上出现不同参数 -> 序号漂移，必须拒绝而不是执行
            arguments={"order_id": "ORD-4", "amount": 999},
            operation=lambda: external.refund({"order_id": "ORD-4", "amount": 999}),
            store=store,
        )
    assert external.executions == 1, "指纹冲突必须 fail-closed，不能执行第二次"


@requires_infra
async def test_hitl_approval_key_semantics_are_unchanged(store):
    """A1 修复不得破坏 HITL 的 ``approval:{approval_id}`` 幂等语义。"""
    from runtime.side_effects import build_tool_idempotency_key

    run_id = f"{prefix}-run5"
    approval_id = "11111111-2222-3333-4444-555555555555"
    external = _ExternalSystem()

    key_a = build_tool_idempotency_key(run_id, f"approval:{approval_id}")
    key_b = build_tool_idempotency_key(run_id, f"approval:{approval_id}")
    other = build_tool_idempotency_key(run_id, "approval:99999999-9999-9999-9999-999999999999")
    assert key_a == key_b
    assert key_a != other, "不同审批必须是不同的键（不得误合并两次合法操作）"

    for _ in range(2):
        await execute_idempotent_operation(
            tool_name="staging_refund",
            operation_key=key_a,
            run_id=run_id,
            thread_id=f"{prefix}-t",
            arguments={"order_id": "ORD-5", "amount": 5},
            operation=lambda: external.refund({"order_id": "ORD-5", "amount": 5}),
            store=store,
        )
    assert external.executions == 1, "同一审批的重复执行必须被去重"


@requires_infra
async def test_content_addressed_key_dedupes_reordered_same_tool_writes(store):
    """Issue #12 Part A：内容寻址键在真实 PG 上对同工具写序列重排去重。

    attempt 1 发 ``[refund(A)]``；attempt 2 重排为 ``[refund(B), refund(A)]``（A 换到
    槽位 1）。位置身份下 A 会拿到新键被再次执行；内容寻址下 A 稳定命中同一个键。
    """
    from runtime.side_effects import build_replay_stable_operation_key

    run_id = f"{prefix}-runA-reorder"
    thread_id = f"{prefix}-threadA-reorder"

    def key(arguments: dict, occurrence: int) -> str:
        return build_replay_stable_operation_key(
            run_id=run_id,
            node_id="tools#0",
            agent="refund_agent",
            tool_name="staging_refund",
            arguments=arguments,
            ordinal=occurrence,
        )

    a = {"order_id": "ORD-A1", "amount": 100}
    b = {"order_id": "ORD-B1", "amount": 200}
    external = _ExternalSystem()

    async def _run(op_key: str, args: dict) -> None:
        await execute_idempotent_operation(
            tool_name="staging_refund",
            operation_key=op_key,
            run_id=run_id,
            thread_id=thread_id,
            arguments=args,
            operation=lambda: external.refund(args),
            store=store,
        )

    # attempt 1: [A]
    await _run(key(a, 0), a)
    # attempt 2（重放、重排）: [B, A] —— A 现在是同一内容的第 0 次出现
    await _run(key(b, 0), b)
    await _run(key(a, 0), a)

    assert external.executions == 2, f"外部只应执行 A、B 各一次: {external.calls}"
    assert external.calls.count(a) == 1, "A 换槽位后必须被内容寻址键去重"
    assert external.calls.count(b) == 1


@requires_infra
async def test_partial_resume_recovers_interrupted_identical_write(store, cleanup_side_effects):
    """Issue #12 Part A **部分恢复**残差：真实 PG 上不得静默吞掉第二笔同参写。

    attempt 1 提出 ``[refund(A), refund(A)]``（两笔独立业务意图）：解析器在**执行任何写
    之前**把两个 occurrence 都预占落账（``reserve_operation``）；第 1 笔（occ0）执行成功
    后 worker 崩溃，occ1 仍是未执行的预占。恢复时只提出一个无业务 ID 的 A，
    无法判断是 A1 重放还是 A2 首次执行，必须 fail-closed；当完整提出 [A1,A2]
    时保留原序号，A1 去重且 A2 执行，合计两笔合法写入。
    """
    from runtime.operation_identity import ContentOccurrenceResolver
    from runtime.side_effects import build_replay_stable_operation_key

    run_id = f"{prefix}-partial"
    thread_id = f"{prefix}-t"
    node_id, agent, tool = "tools#0", "refund_agent", "staging_refund"
    args = {"order_id": "ORD-PR", "amount": 100}
    external = _ExternalSystem()

    def key(occ: int) -> str:
        return build_replay_stable_operation_key(
            run_id=run_id,
            node_id=node_id,
            agent=agent,
            tool_name=tool,
            arguments=args,
            ordinal=occ,
        )

    cleanup_side_effects.extend([key(0), key(1)])

    async def _run(k: str) -> None:
        await execute_idempotent_operation(
            tool_name=tool,
            operation_key=k,
            run_id=run_id,
            thread_id=thread_id,
            arguments=args,
            operation=lambda: external.refund(args),
            store=store,
        )

    # attempt 1 预解析：本轮两个同内容 occurrence 全部预占后，只执行 occ0。
    resolver = ContentOccurrenceResolver(
        run_id=run_id, node_id=node_id, agent=agent, store=store, thread_id=thread_id
    )
    assert resolver.assign([(tool, args), (tool, args)]) == [0, 1]
    await _run(key(0))
    assert external.executions == 1
    assert store.get(tool, key(1))["status"] == "PENDING", "occ1 必须已作为未执行预占落账"

    # Shortened replay with one identical proposal is indistinguishable:
    # it may mean "replay A1" or "execute A2". It must fail closed.
    from runtime.operation_identity import OperationIdentityUnavailable

    resolver2 = ContentOccurrenceResolver(
        run_id=run_id, node_id=node_id, agent=agent, store=store, thread_id=thread_id
    )
    with pytest.raises(OperationIdentityUnavailable, match="ambiguous partial replay"):
        resolver2.assign([(tool, args)])
    assert external.executions == 1, "ambiguous shortened replay must not issue another refund"

    # Full [A1, A2] replay preserves occurrence order: A1 dedupes, A2 executes.
    resolver3 = ContentOccurrenceResolver(
        run_id=run_id, node_id=node_id, agent=agent, store=store, thread_id=thread_id
    )
    assert resolver3.assign([(tool, args), (tool, args)]) == [0, 1]
    await _run(key(0))
    await _run(key(1))
    assert external.executions == 2, "full replay must recover exactly the missing refund"
