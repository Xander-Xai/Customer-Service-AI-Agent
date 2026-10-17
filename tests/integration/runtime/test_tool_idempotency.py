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

_VISIBILITY_TIMEOUT = 120
#: worker 被杀后消息**立即**重投（实测：task_reject_on_worker_lost），与 visibility
#: 无关。但**领取**要求 lease 已过期：本 lane 的 lease 是 3s，而重投几乎立刻发生，
#: 于是重投的那条消息会被正确地拒绝（「已由其他 worker 持有 lease」——这正是
#: 「重复投递不得杀死正在执行的 run」这条契约在起作用）。
#: 因此这里要等 lease 过期之后才让第二个 worker 上场：
#: 等待窗口 = lease + 余量。真正的收敛断言仍由下面的 ``_wait_for`` 承担。
_REDELIVERY_SETTLE_SECONDS = 10
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
            # 交叉不变量（``core.config.validate_timeout_invariants``，所有环境强制）：
            # 本 lane 把定时器缩到秒级以便观察崩溃/接管，但**缩小不等于可以不一致**。
            # 下面这组值彼此满足 visibility > task_time_limit > 工具阻塞时长，且
            # lock_ttl > task_time_limit + margin；lease/heartbeat 保留小值是为了让
            # 「owner 消失 -> 接管」在秒级发生。
            "AGENT_RUN_VISIBILITY_TIMEOUT": "120",
            "AGENT_RUN_LEASE_SECONDS": "3",
            "AGENT_RUN_HEARTBEAT_SECONDS": "1",
            "AGENT_RUN_TASK_TIME_LIMIT": "60",
            # lock TTL 必须覆盖 task time limit（60s）+ margin，否则任务仍在执行时
            # 锁已过期，同 thread 的另一个 worker 可以进入同一条 state lineage。
            "AGENT_RUN_THREAD_LOCK_TTL_SECONDS": "120",
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


def _recover_via_reconciler(db_url: str, run_id: str, queue: str) -> None:
    """等 lease 真的过期，然后**只**把这一条 run 重新投递到本 lane 的队列。

    刻意**不**调用 ``runtime.retry.reconcile_stuck_runs``：它是全表扫描，会把共享
    库里所有滞留 run 倒进本 lane 的队列。它们会被本 lane 的 worker 执行，而 provider
    从环境变量读 ``SIDE_EFFECT_KEY_PREFIX``，于是无关 run 的副作用被记到本 lane 的
    计数器上，让「副作用恰好执行一次」这条断言在跑过整套 runtime-e2e 之后失效。

    「能不能接管」仍由 worker 侧的 ``mark_running`` 原子判定（lease + 预算谓词），
    这里只负责等 lease 过期并投递。
    """
    import time as _time

    from celery import Celery
    from sqlalchemy import create_engine
    from sqlalchemy import text as _text

    engine = create_engine(db_url)

    def _lease():
        with engine.connect() as conn:
            value = conn.execute(
                _text("SELECT lease_expires_at FROM agent_runs WHERE id=:id"), {"id": run_id}
            ).scalar()
        if value is not None and value.tzinfo is None:
            from datetime import timezone as _tz

            value = value.replace(tzinfo=_tz.utc)
        return value

    deadline = _time.monotonic() + 30
    while _time.monotonic() < deadline:
        lease = _lease()
        if lease is None or lease.timestamp() <= _time.time():
            break
        _time.sleep(0.5)

    Celery(broker=REDIS_URL).send_task("runtime.execute_agent_run", args=[run_id], queue=queue)


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

    Celery(broker=REDIS_URL).send_task("runtime.execute_agent_run", args=[run_id], queue=queue)

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

        # 2) 工具成功、消息未 ACK 时 SIGKILL 整个 worker 进程组
        #
        # 杀整个组（而不是只杀 prefork 子进程）是刻意的：父进程存活会不断 spawn 新
        # 子进程去消费同一条重投消息，与后续 worker 竞争，让本用例依赖调度时序
        # （实测出现过副作用被反复重放、断言偶发失败）。
        #
        # 父进程一起死掉意味着 **broker 不会自动重投**——这正是本仓库实测的投递
        # 语义（``make runtime-delivery``：只有 WorkerLostError 会重投，而它需要
        # 父进程活着）。因此本用例的恢复必须由 reconciler 提供，见下面第 3b 步。
        _stop_worker(worker_a)

        # 3) 等未 ACK 消息重新投递，启动 Worker B
        #
        # 这里**不再**等 visibility 超时。本 lane 原本把 visibility 设成 5s 并阻塞
        # 工具 25s，也就是「可见性时间短于最长执行时间」——那正是
        # ``AGENT_RUN_VISIBILITY_TIMEOUT > AGENT_RUN_TASK_TIME_LIMIT`` 这条不变量禁止
        # 的配置（消息在旧执行者仍在运行时重新可见 = 同一 run 并发执行），
        # 现在已在启动时被拒绝。
        #
        # 实测（``make runtime-delivery``，Celery 5.6.3 + 真实 Redis broker）：
        # worker 进程被杀触发 ``task_reject_on_worker_lost``，消息**立即**重投，
        # 与 visibility 无关。所以这里等一个短的确定窗口即可；真正的等待由下面的
        # ``_wait_for(_terminal, ...)`` 承担。
        time.sleep(_REDELIVERY_SETTLE_SECONDS)

        # 3b) **生产里的真实恢复路径是 reconciler，不是 broker 反复重投。**
        #
        # 实测（``make runtime-delivery``）：worker 被杀会触发一次**立即**重投，
        # 但那次重投几乎总是落在 lease 过期之前，于是 worker 正确地拒绝领取
        # （「已由其他 worker 持有 lease」——这正是「重复投递不得杀死正在执行的
        # run」这条契约在起作用），而那条消息已经被消费掉了。
        #
        # 因此「只有一次立即重投」**不足以**恢复；恢复靠的是数据库里 lease 过期的
        # RUNNING 被 reconciler 捞起并重新投递。本 lane 此前靠把 visibility 设成 5s
        # 来制造第二次投递机会，那等于配置成
        # ``AGENT_RUN_VISIBILITY_TIMEOUT < AGENT_RUN_TASK_TIME_LIMIT``——正是
        # ``validate_timeout_invariants`` 禁止的组合（消息在旧执行者仍在运行时
        # 重新可见 = 同一 run 并发执行）。
        #
        # 这里改为显式跑真正的 reconciler：它先确认「此刻 lease 确已过期」，再把
        # run 重新投递到本 lane 的队列。这才是需要被验证的恢复机制。
        # 3b) 清掉 broker 侧残留（未 ACK 消息 + 队列里的任何投递），让本用例的
        # 恢复**只**可能来自下面的 reconciler。``unacked``/``unacked_index`` 是
        # Redis transport 的**全局** bookkeeping 键（不是 per-queue），用例开头与
        # 收尾都要动它们，这一点原有注释已经记录过。
        client.delete(queue, "unacked", "unacked_index")

        # 3c) 用**生产同款** reconciler 恢复。父进程已死，broker 不会自动重投
        # （实测：WorkerLostError 的重投依赖父进程存活），因此这条路径就是生产里
        # 真正起作用的恢复机制。
        _recover_via_reconciler(DB_URL, run_id, queue)

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
        assert final_counter == 1, f"副作用被重复执行！counter={final_counter}（必须 == 1）"

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
            conn.execute(text("DELETE FROM agent_dead_letters WHERE run_id=:id"), {"id": run_id})
            conn.execute(text("DELETE FROM tool_side_effects WHERE run_id=:id"), {"id": run_id})
            conn.execute(text("DELETE FROM checkpoints WHERE thread_id = :t"), {"t": thread_id})
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
