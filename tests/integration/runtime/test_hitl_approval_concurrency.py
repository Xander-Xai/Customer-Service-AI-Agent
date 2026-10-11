"""HITL 治理的**并发与恢复**契约 —— 真实 PostgreSQL。

已有的 ``tests/unit/test_hitl_approval.py`` 用 SQLite 覆盖状态机 / TTL / 职责分离 /
幂等的**顺序**语义；本文件覆盖它们证明不了、且失效后果最严重的两类：

1. **并发审批**：两个审批人几乎同时对同一条审批下决策。顺序幂等（第二次是 no-op）
   证明不了并发安全 —— 真正的答案是「数据库级 CAS 在并发下是否恰好一个赢家」。
   SQLite 的 ``StaticPool`` 是单连接，测不出竞争；生产用的是 PostgreSQL，因此这里
   就用**真实 PostgreSQL + 真实线程**。

2. **重试 / worker 恢复不得绕过审批**：at-least-once 投递 + 崩溃恢复意味着同一个 run
   会被反复执行。若恢复路径在没有决策时就把副作用执行掉，「审批」就只是一个可绕过
   的检查点。断言必须是：**没有决策 -> 一次都不执行**。

沿用本目录约定：``TEST_DISTRIBUTED_DB_URL`` 未设置时整体 skip，
绝不让「没跑」看起来像「通过」。
"""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not DB_URL,
        reason="TEST_DISTRIBUTED_DB_URL 未设置；需要真实 PostgreSQL（并发语义无法用 mock 证明）",
    ),
]


def _store(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)


def _service(engine):
    from core.hitl.approval_service import ApprovalService

    return ApprovalService(session_factory=_store(engine))


def _proposal(engine, *, payload=None, **overrides):
    """创建审批。``payload`` 可显式传入以复现「同一提案重放」。"""
    payload = payload or {
        "run_id": f"run-{uuid.uuid4().hex[:12]}",
        "thread_id": f"thread-{uuid.uuid4().hex[:8]}",
        "action": "staging_refund",
        "risk_level": "high",
        "proposal": {"order_id": f"O-{uuid.uuid4().hex[:8]}", "amount": 100},
        "user_id": "customer-1",
        "agent": "order_agent",
    }
    payload.update(overrides)
    svc = _service(engine)
    return svc.create_or_get(**payload)


class TestConcurrentDecisions:
    def test_exactly_one_of_two_conflicting_decisions_wins(self, pg_engine):
        """approve 与 reject 同时提交：只能有一个生效。

        若实现是「读状态 -> 判断 -> 写状态」，两个线程可能都读到 PENDING 并都写入，
        审计里就会出现互相矛盾的两条决策。``decide()`` 用的是
        ``UPDATE ... WHERE status = PENDING`` 的数据库级 CAS，因此恰好一个赢家。
        """
        from core.hitl.approval_service import (
            DECISION_APPROVE,
            DECISION_REJECT,
            STATUS_PENDING,
        )

        record = _proposal(pg_engine)
        approval_id = record["approval_id"]
        barrier = threading.Barrier(2)

        def decide(decision: str):
            barrier.wait(timeout=30)
            return _service(pg_engine).decide(
                approval_id, reviewer_id=f"reviewer-{decision}", decision=decision
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(decide, DECISION_APPROVE),
                pool.submit(decide, DECISION_REJECT),
            ]
            outcomes = [f.result() for f in futures]

        winners = [o for o in outcomes if o[1]]
        assert len(winners) == 1, f"并发决策出现 {len(winners)} 个赢家（CAS 失效）：{outcomes}"

        final = _service(pg_engine).get(approval_id)
        assert final["status"] != STATUS_PENDING, "并发决策后审批仍停留在 PENDING"

    def test_concurrent_duplicate_decisions_converge_to_one(self, pg_engine):
        """客户端重试风暴：同一个决策被并发提交 N 次，只应生效一次。"""
        from core.hitl.approval_service import DECISION_APPROVE

        record = _proposal(pg_engine)
        approval_id = record["approval_id"]
        workers = 8
        barrier = threading.Barrier(workers)

        def decide():
            barrier.wait(timeout=30)
            return _service(pg_engine).decide(
                approval_id, reviewer_id="reviewer-a", decision=DECISION_APPROVE
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = [f.result() for f in [pool.submit(decide) for _ in range(workers)]]

        assert (
            sum(1 for _, newly in outcomes if newly) == 1
        ), f"并发重复决策中生效了 {sum(1 for _, n in outcomes if n)} 次，应恰好 1 次"

    def test_concurrent_consumers_get_exactly_one_resume_payload(self, pg_engine):
        """多个 worker 同时接手同一个已批准 run：只有一次能拿到「执行已批准副作用」的信号。

        这是「恢复不得绕过、也不得重复执行审批」的落点：`consume_resume` 的
        ``WHERE resumed_at IS NULL`` 保证只被消费一次。它与 side-effect ledger
        （``operation_key = run_id:approval:{id}``）是两道**独立**防线 ——
        审批防「不该做的被做了」，ledger 防「做了一次被重做」。
        """
        from core.hitl.approval_service import DECISION_APPROVE

        record = _proposal(pg_engine)
        approval_id = record["approval_id"]
        run_id = record["run_id"]
        _service(pg_engine).decide(approval_id, reviewer_id="reviewer-a", decision=DECISION_APPROVE)

        workers = 6
        barrier = threading.Barrier(workers)

        def consume():
            barrier.wait(timeout=30)
            return _service(pg_engine).consume_resume(run_id)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            payloads = [f.result() for f in [pool.submit(consume) for _ in range(workers)]]

        non_null = [p for p in payloads if p is not None]
        assert (
            len(non_null) == 1
        ), f"并发恢复拿到 {len(non_null)} 份载荷（应恰好 1 份）—— 重复执行已批准副作用"


class TestRecoveryCannotBypassApproval:
    def test_recovery_without_decision_yields_no_payload(self, pg_engine):
        """没有任何决策时，恢复路径拿不到任何执行许可。

        崩溃恢复会重放 ``Command(resume=...)``；若此时审批还没人决策，载荷必须是空的，
        而不是「默认批准」。这是审批治理与 worker 恢复之间的关键接缝。
        """
        from core.hitl.approval_service import STATUS_PENDING

        record = _proposal(pg_engine)
        assert record["status"] == STATUS_PENDING
        assert _service(pg_engine).consume_resume(record["run_id"]) is None

    def test_consumed_decision_stays_consumed(self, pg_engine):
        """重复恢复不会让已消费的决策复活。"""
        from core.hitl.approval_service import DECISION_APPROVE

        record = _proposal(pg_engine)
        svc = _service(pg_engine)
        svc.decide(record["approval_id"], reviewer_id="reviewer-a", decision=DECISION_APPROVE)

        assert svc.consume_resume(record["run_id"]) is not None
        assert svc.consume_resume(record["run_id"]) is None, "已消费的决策被第二次取回"

    def test_node_replay_reuses_the_same_decided_approval(self, pg_engine):
        """节点重放（LangGraph 恢复时从头重放）复用同一条已决策审批。

        若重放新建了第二条 PENDING 审批，run 会永久停在 WAITING_APPROVAL ——
        表现为「批过了却还在等审批」的死等。
        """
        from core.hitl.approval_service import DECISION_APPROVE, STATUS_APPROVED

        payload = {
            "run_id": f"run-{uuid.uuid4().hex[:12]}",
            "thread_id": f"thread-{uuid.uuid4().hex[:8]}",
            "action": "staging_refund",
            "risk_level": "high",
            "proposal": {"order_id": f"O-{uuid.uuid4().hex[:8]}", "amount": 100},
            "user_id": "customer-1",
            "agent": "order_agent",
        }
        svc = _service(pg_engine)
        record = svc.create_or_get(**payload)
        svc.decide(record["approval_id"], reviewer_id="reviewer-a", decision=DECISION_APPROVE)

        # 节点重放：完全相同的 run + action + proposal，再次 create_or_get。
        replayed = svc.create_or_get(**payload)
        assert replayed["approval_id"] == record["approval_id"]
        assert replayed["status"] == STATUS_APPROVED, "重放把已决策审批回退成待审批"
