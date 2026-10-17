"""Celery 投递语义的**真实**验收（真实 Redis broker + 真实 Celery worker 进程）。

为什么必须有这个文件
--------------------
本仓库此前把「让异常逃逸以触发 broker 重新投递」当作恢复手段，散落在
``runtime/celery_app.py`` / ``runtime/executor.py`` / ``runtime/retry.py`` 的注释、
``CLAUDE.md`` 和一个单元测试的 docstring 里。这个前提是**错的**。

读 Celery 源码可以推断，但推断不算证据。本文件用真实 broker + 真实 worker 子进程
把结论钉死：任务被**真的执行了几次**、消息被 ACK 还是被 reject、重新投递时
task_id 是否变化。

每类失败都在**独立的新 worker** 上跑
------------------------------------
``Reject(requeue=True)`` 会立刻把消息塞回队首（实测 5 秒内 80 次），会把这个
worker 的预取队列灌满并掩盖后续场景的行为。共用 worker 得到的矩阵是不可信的。

不在这里断言什么
----------------
不解析 worker 日志里的 "rejected"/"acknowledged" 字样——那是 Celery 的内部措辞，
不是契约。断言的是**可观测后果**：执行次数、task_id、broker 队列深度。

skip 语义
---------
需要 ``TEST_REDIS_URL``（真实 Redis）。未设置时 skip ——「没跑」不允许被读成
「跑过了」，这与本目录其他文件一致。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE_MODULE = "tests.integration.runtime.celery_delivery_probe"


def _worker_env(redis_url: str) -> dict[str, str]:
    env = dict(os.environ)
    env["CELERY_BROKER_URL"] = redis_url
    env["REDIS_URL"] = redis_url
    env["DEV_MODE"] = "true"
    # 锁与事件流不是本文件要验证的东西，关掉以免引入无关的 Redis 往返。
    env["AGENT_RUN_THREAD_LOCK_ENABLED"] = "False"
    # 硬/软时间上限要短，否则每个场景都要等分钟级。
    env["AGENT_RUN_TASK_TIME_LIMIT"] = "4"
    env["AGENT_RUN_TASK_SOFT_TIME_LIMIT"] = "3"
    # 探针任务**不调用任何工具**，因此工具超时在这里显式关闭（0 = 关闭）。
    # 这不是绕过校验：把 4s 的 task limit 与默认 30s 的工具超时放在一起，本来就违反
    # `TOOL_EXECUTION_TIMEOUT_SECONDS < AGENT_RUN_TASK_TIME_LIMIT`——那条关系在生产分支
    # 早已强制，现在被提升为所有环境强制（见 core.config.validate_timeout_invariants）。
    # 正确做法是让本 lane 自己声明「不测工具超时」，而不是把不变量降级成警告。
    env["TOOL_EXECUTION_TIMEOUT_SECONDS"] = "0"
    return env


class _Worker:
    """一个真实的 Celery worker 子进程（prefork，concurrency=1）。"""

    def __init__(self, redis_url: str, queue: str, env: dict[str, str]):
        self.queue = queue
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "runtime.celery_app:celery_app",
                "worker",
                "--loglevel=warning",
                "--concurrency=1",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "-Q",
                queue,
                f"--include={PROBE_MODULE}",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def broker(redis_url: str):
    """真实 Redis + 每个场景独立的 queue/worker。"""
    import redis as redis_lib

    rc = redis_lib.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
    rc.ping()
    queue = f"celery_delivery_{uuid.uuid4().hex[:10]}"
    env = _worker_env(redis_url)

    def publish(task: str, key: str) -> str:
        code = (
            "import os,sys;sys.path.insert(0, os.getcwd());"
            "from runtime.celery_app import celery_app;"
            f"r = celery_app.send_task({task!r}, args=[{key!r}], queue={queue!r});"
            "print(r.id)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        lines = [ln for ln in (out.stdout or "").strip().splitlines() if ln.strip()]
        assert lines, f"publish failed: {out.stderr[-500:]}"
        return lines[-1].strip()

    def start() -> _Worker:
        return _Worker(redis_url, queue, env)

    def wait_ready(rc, key_prefix: str, timeout: float = 90.0) -> None:
        """用 sentinel 任务确认 worker 真的在消费（不靠 inspect ping 的节点名匹配）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            publish("probe.success", key_prefix)
            time.sleep(1.0)
            if int(rc.get(f"{key_prefix}:executions") or 0) > 0:
                return
        raise AssertionError("worker 在超时窗口内没有消费任何任务")

    yield {"rc": rc, "queue": queue, "publish": publish, "start": start, "wait_ready": wait_ready}
    rc.delete(f"{queue}")
    for key in rc.scan_iter(match="probe-*"):
        rc.delete(key)
    for key in rc.scan_iter(match="sent-*"):
        rc.delete(key)


def _observe(broker, task: str, wait_s: float) -> dict:
    rc = broker["rc"]
    key = f"probe-{uuid.uuid4().hex[:10]}"
    worker = broker["start"]()
    try:
        broker["wait_ready"](rc, f"sent-{uuid.uuid4().hex[:8]}")
        published = broker["publish"](task, key)
        time.sleep(wait_s)
        return {
            "published_task_id": published,
            "executions": int(rc.get(f"{key}:executions") or 0),
            "task_ids": rc.lrange(f"{key}:task_ids", 0, -1),
            "queue_depth": rc.llen(broker["queue"]),
        }
    finally:
        worker.stop()


# ---------------------------------------------------------------------------
# 1. 成功：ACK，不重投
# ---------------------------------------------------------------------------


def test_success_is_acknowledged_and_not_redelivered(broker):
    r = _observe(broker, "probe.success", 5.0)
    assert r["executions"] == 1
    assert r["queue_depth"] == 0


# ---------------------------------------------------------------------------
# 2-4. 关键结论：普通异常系**不重投**，消息被丢弃
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "label"),
    [
        ("probe.ordinary_exception", "普通 Exception"),
        ("probe.retry_publication_error", "RetryPublicationError（executor 依赖它逃逸）"),
    ],
)
def test_ordinary_exception_is_discarded_not_redelivered(broker, task, label):
    """本仓库的恢复设计依赖的前提，必须被实测钉死。

    ``task_acks_on_failure_or_timeout=False`` 只让 Celery 不 ACK 失败任务；它随后走
    ``Request.on_failure`` 的 ``self.reject(requeue=False)``，消息被**丢弃**。
    因此「让异常逃逸以触发 broker 重新投递」不成立。
    """
    r = _observe(broker, task, 6.0)
    assert r["executions"] == 1, f"{label}: 只应执行一次"
    assert r["queue_depth"] == 0, f"{label}: 消息被丢弃，队列里没有待重投的消息"
    assert len(set(r["task_ids"])) == 1


def test_soft_time_limit_is_discarded_not_redelivered(broker):
    """软时间上限是普通异常子类，同样走 reject(requeue=False)。"""
    r = _observe(broker, "probe.sleep_softly", 12.0)
    assert r["executions"] == 1
    assert r["queue_depth"] == 0


def test_explicit_reject_requeue_redelivers_without_backoff(broker):
    """``Reject(requeue=True)`` 确实重投，而且**立刻**重投。

    实测 5 秒内 80 次。这个形状说明为什么裸 reject 不能当恢复策略：它会烧穿
    attempt 预算并制造热循环。恢复必须靠数据库状态 + 有退避的周期扫描。
    """
    r = _observe(broker, "probe.reject_requeue", 5.0)
    assert r["executions"] > 5, "Reject(requeue=True) 应立即反复重投"
    assert len(set(r["task_ids"])) == 1, "是同一条消息被重投，task_id 不变"


def test_explicit_reject_no_requeue_is_discarded(broker):
    """``Reject(requeue=False)``：任务体**跑过一次**，消息随后被丢弃。

    与 requeue=True 对照可知 ``requeue`` 标志才是「是否重投」的唯一决定因素，
    且两者都不经过 ack。
    """
    r = _observe(broker, "probe.reject_no_requeue", 5.0)
    assert r["executions"] == 1
    assert r["queue_depth"] == 0
    assert len(set(r["task_ids"])) == 1


# ---------------------------------------------------------------------------
# 5. 唯一可靠的 broker 重投路径：worker 进程死亡
# ---------------------------------------------------------------------------


def test_worker_lost_redelivers_the_same_message(broker):
    """SIGKILL 掉执行子进程 -> WorkerLostError -> 消息重新投递，task_id 不变。

    这是 at-least-once 真正生效的场景，也是 ``make runtime-chaos`` 验证的那条路。
    """
    r = _observe(broker, "probe.suicide", 30.0)
    assert r["executions"] > 1, "worker 被杀后消息应被重新投递"
    assert len(set(r["task_ids"])) == 1, "重投的是同一条消息，task_id 不变"


# ---------------------------------------------------------------------------
# 6. 恢复闭环：周期 reconciler 必须真的被调度
# ---------------------------------------------------------------------------


def test_recovery_does_not_depend_on_broker_redelivery(broker):
    """把整条链路钉成一句话：broker 不重投，恢复靠 DB + 周期扫描。

    这不是重复前面的用例，而是把「所以恢复闭环必须落在哪」写成断言：
    ``beat_schedule`` 存在 + reconciler 任务已注册。删掉任何一个，
    上面那些「消息被丢弃」的 run 就彻底没有自动恢复手段。
    """
    import runtime.tasks  # noqa: F401  # 惰性 imports 需先解析
    from runtime.celery_app import celery_app

    assert "reconcile-stuck-runs" in celery_app.conf.beat_schedule
    assert "runtime.reconcile_stuck_runs" in celery_app.tasks
