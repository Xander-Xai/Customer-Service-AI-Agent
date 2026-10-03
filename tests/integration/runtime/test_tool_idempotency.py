"""Gate 11：副作用工具的幂等保护（**关键验收**，失败即整体 FAIL）。

场景（at-least-once delivery 无法避免重复投递，所以副作用必须自身幂等）::

    Worker A: 执行副作用工具 -> 外部系统已写入（counter=1）
              -> 工具成功、但消息还没 ACK 时被 SIGKILL
    Broker:   未 ACK 消息重新投递
    Worker B: 从 checkpoint 续跑，重做该节点
              -> ledger 命中 SUCCEEDED -> 返回历史结果
              -> 外部系统**不得**被再次写入

验收：
  1. ``idem:counter == 1``（绝不能是 2）；
  2. ``tool_side_effects`` 里存在该 ``(tool_name, operation_key)`` 且 status=SUCCEEDED；
  3. 重投确实发生过（``idem:tool_invocations >= 2``、``idem:ledger_hits >= 1``）——
     否则 counter==1 只是因为压根没重跑；
  4. 最终 run SUCCEEDED。

``agent_tool_idempotency_hit_total`` 的递增由同目录
``test_tool_idempotency_metric.py`` 在进程内断言（worker 进程的 Prometheus
registry 外部无法读取）。

运行::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_tool_idempotency.py -q
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (DB_URL and REDIS_URL), reason=INFRA_REASON),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKER_CMD = [sys.executable, "-m", "tests.integration.celery_worker_runner"]
_PROVIDER = "tests.integration.side_effect_provider:provide"

_VISIBILITY_TIMEOUT = 5
_BLOCK_SECONDS = 25


#: 本次 run 专用的 Redis key 名称。**必须**按 run 打标签（见 ``_worker_env``
#: 的 SIDE_EFFECT_KEY_PREFIX）：这四个 key 曾是全局固定的
#: ``idem:counter / idem:ledger_hits / idem:phase / idem:tool_invocations``。
#: 用例开头的 ``client.delete`` 只能清掉**当时**的值；一个仍存活的 worker 进程
#: 之后继续写同一组 key，本用例的计数断言就会失真。是否就是某次
#: ``hits >= 1`` 偶发失败的确切交错尚未复现（未抓到失败现场），但「共享命名空间
#: + 残留进程」这一前提本身已被证实：``finally`` 漏掉 worker_a 时确实会留下
#: 存活的 Celery 进程。按 run 隔离后本用例不再依赖「机器上没有别人碰过这些 key」。
def _redis_keys(prefix: str) -> tuple[str, ...]:
    return (
        f"{prefix}:counter",
        f"{prefix}:ledger_hits",
        f"{prefix}:phase",
        f"{prefix}:tool_invocations",
    )


def _worker_env(queue: str, key_prefix: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": DB_URL,
            "LANGGRAPH_CHECKPOINT_DATABASE_URL": DB_URL,
            "REDIS_URL": REDIS_URL,
            "DEV_MODE": "true",
            "AGENT_RUN_DISPATCH": "celery",
            "AGENT_RUN_QUEUE": queue,
            "AGENT_RUN_VISIBILITY_TIMEOUT": str(_VISIBILITY_TIMEOUT),
            "AGENT_RUN_LEASE_SECONDS": "3",
            "AGENT_RUN_HEARTBEAT_SECONDS": "1",
            "AGENT_RUN_THREAD_LOCK_TTL_SECONDS": "3",
            "AGENT_RUN_THREAD_LOCK_BACKEND": "redis",
            "AGENT_RUN_RUNTIME_PROVIDER": _PROVIDER,
            "SIDE_EFFECT_BLOCK_SECONDS": str(_BLOCK_SECONDS),
            "SIDE_EFFECT_KEY_PREFIX": key_prefix,
            "LANGGRAPH_CHECKPOINT_BACKEND": "postgres",
        }
    )
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def _start_worker(env: dict[str, str], log_path: str) -> subprocess.Popen:
    log = open(log_path, "w")  # noqa: SIM115 - closed in _stop_worker
    proc = subprocess.Popen(
        _WORKER_CMD,
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._csai_log = log  # type: ignore[attr-defined]
    return proc


def _stop_worker(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=15)
    log = getattr(proc, "_csai_log", None)
    if log is not None:
        log.close()


def _wait_for(predicate, timeout: float):
    """轮询直到 predicate 给出 truthy 结果；超时返回 ``None``。

    超时**不**返回最后一次求值。这一点不是风格问题：

    - 旧实现 ``return predicate()`` 会把终态判定 predicate 的 ``False`` 当成
      「拿到结果了」。调用方的 ``assert final is not None`` 因此通过，接着
      ``final[0]`` 抛 ``TypeError: 'bool' object is not subscriptable``——
      「run 没进终态」这个本来可诊断的失败被替换成一个看不懂的崩溃。
    - 而且 deadline 已经过了还要再查一次库/Redis，是超时之后的额外 I/O。

    truthy 的真实 payload（tuple/row/对象）原样返回，调用方依赖它而不是一个
    ``True``。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.3)
    return None


@pytest.mark.timeout(300)
def test_gate11_side_effect_tool_executes_exactly_once_across_worker_kill():
    import redis as redis_lib
    from sqlalchemy import create_engine, text

    from db.models import AgentDeadLetter, AgentRun, ToolSideEffect

    engine = create_engine(DB_URL)
    for table in (AgentRun, AgentDeadLetter, ToolSideEffect):
        table.__table__.create(engine, checkfirst=True)

    tag = uuid.uuid4().hex[:8]
    run_id = f"idem-{tag}"
    thread_id = f"T-idem-{tag}"
    queue = f"agent_runs_test_{tag}"
    key_prefix = f"idem:{tag}"
    redis_keys = _redis_keys(key_prefix)

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.delete("unacked", "unacked_index", queue, *redis_keys)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runs "
                "(id, thread_id, session_id, status, query, attempt, max_attempts, "
                " created_at, queued_at, updated_at) "
                "VALUES (:id, :tid, :tid, 'QUEUED', 'refund-me', 0, 3, now(), now(), now())"
            ),
            {"id": run_id, "tid": thread_id},
        )

    from celery import Celery

    Celery(broker=REDIS_URL).send_task(
        "runtime.execute_agent_run", args=[run_id], queue=queue
    )

    def counter():
        return int(client.get(f"{key_prefix}:counter") or 0)

    def status():
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT status, attempt, worker_id FROM agent_runs WHERE id=:id"),
                {"id": run_id},
            ).fetchone()

    env = _worker_env(queue, key_prefix)
    # 记录所有已启动的 worker：``finally`` 必须收掉**每一个**，包括崩溃窗口之前
    # 就断言失败的情况。此前只 stop 了 worker_b，worker_a 在提前失败时会泄漏成
    # 存活的 Celery 进程（实测注入一次提前失败即残留 2 个孤儿 worker），继续消费
    # 队列并写共享 key，污染后续所有运行。
    started: list[subprocess.Popen] = []
    worker_a = _start_worker(env, "/tmp/csai-idem-worker-a.log")
    started.append(worker_a)
    worker_b = None
    try:
        # 1) 等副作用真实落地（counter == 1），此时工具已成功但图还在阻塞
        landed = _wait_for(lambda: counter() >= 1, 90)
        assert landed, "副作用工具从未执行"
        assert counter() == 1, f"崩前 counter 应为 1，实际 {counter()}"

        # ledger 已记录 SUCCEEDED
        ledger = _wait_for(
            lambda: _ledger_row(engine, thread_id) is not None
            and _ledger_row(engine, thread_id)[2] == "SUCCEEDED",
            20,
        )
        assert ledger, "ledger 未记录 SUCCEEDED"

        # 2) 工具成功、消息未 ACK 时 SIGKILL
        _stop_worker(worker_a)

        # 3) 等未 ACK 消息重新可见，启动 Worker B
        time.sleep(_VISIBILITY_TIMEOUT + 3)
        worker_b = _start_worker(env, "/tmp/csai-idem-worker-b.log")
        started.append(worker_b)

        # 返回 **None** 而不是 False：``_wait_for`` 超时后返回 None，若 predicate
        # 给的是 False，"没进终态"就会被当成"拿到了结果"。
        def _terminal():
            r = status()
            if r is not None and r[0] in ("SUCCEEDED", "FAILED", "DEAD_LETTER"):
                return r
            return None

        final = _wait_for(_terminal, 150)
        assert final is not None, f"run 未进入终态: {status()}"
        assert final[0] == "SUCCEEDED", f"未恢复成功: {final}"
        assert final[1] >= 2, f"应发生重投重做，实际 attempt={final[1]}"

        # 4) 关键断言：副作用只发生了一次
        final_counter = counter()
        assert final_counter == 1, (
            f"副作用被重复执行！counter={final_counter}（必须 == 1）"
        )

        # 5) 并且确实重投过（否则 counter==1 只是因为没重跑）
        # ``ledger_hits`` 由 provider 在**观测到副作用计数器没变**时递增，
        # 即「execute_raw 返回了 ledger 历史结果、handler 未执行」，因此它证明的
        # 是 ledger 真的生效，而不是「节点又跑了一遍」。
        invocations = int(client.get(f"{key_prefix}:tool_invocations") or 0)
        hits = int(client.get(f"{key_prefix}:ledger_hits") or 0)
        assert invocations >= 2, f"工具应被再次调用，实际 {invocations} 次"
        assert hits >= 1, f"第二次调用应命中 ledger，实际命中 {hits} 次"

        # 6) ledger 只有一条 SUCCEEDED 记录（operation_key = run_id:tool_call_id）
        row = _ledger_row(engine, thread_id)
        assert row is not None
        assert row[0] == "increment_counter"
        assert row[1] == f"{run_id}:{thread_id}:refund"
        assert row[2] == "SUCCEEDED"
        with engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM tool_side_effects "
                    "WHERE run_id=:rid AND tool_name='increment_counter'"
                ),
                {"rid": run_id},
            ).scalar()
        assert count == 1, f"ledger 不应出现重复行，实际 {count}"
    finally:
        # 收掉**所有**已启动的 worker（含崩溃窗口之前就失败的情况），杜绝孤儿
        # Celery 进程存活并继续写共享 key。
        for proc in started:
            _stop_worker(proc)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_runs WHERE id=:id"), {"id": run_id})
            conn.execute(
                text("DELETE FROM agent_dead_letters WHERE run_id=:id"), {"id": run_id}
            )
            conn.execute(
                text("DELETE FROM tool_side_effects WHERE run_id=:id"), {"id": run_id}
            )
            conn.execute(
                text("DELETE FROM checkpoints WHERE thread_id = :t"), {"t": thread_id}
            )
        client.delete(*redis_keys, queue, "unacked", "unacked_index")
        client.close()


def _ledger_row(engine, thread_id: str):
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT tool_name, operation_key, status FROM tool_side_effects "
                "WHERE thread_id=:t LIMIT 1"
            ),
            {"t": thread_id},
        ).fetchone()
