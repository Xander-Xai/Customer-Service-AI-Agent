"""重试退避策略：指数退避 + 抖动，硬上限。

禁止无限 autoretry：attempt 由 ``AGENT_RUN_MAX_ATTEMPTS`` 约束。
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from .repository import ORPHAN_GRACE_SECONDS


def compute_backoff(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter: float = 0.3,
    rng: random.Random | None = None,
) -> float:
    """返回下一次重试延迟（秒），带 jitter。

    attempt=0 时延迟约 base_delay，之后指数增长，封顶 max_delay。
    """
    attempt = max(0, int(attempt))
    rng_obj: Any = rng if rng is not None else random
    exponential = min(max_delay, base_delay * (2**attempt))
    if jitter > 0:
        exponential = exponential + float(rng_obj.uniform(0.0, jitter * exponential))
    return float(round(min(max_delay, exponential), 3))


async def reconcile_stuck_runs(
    service: Any,
    *,
    dispatcher: Any = None,
    limit: int = 100,
    now: datetime | None = None,
    reclaim_stale_running: bool = True,
    reclaim_stale_pending: bool = True,
    pending_grace_seconds: int = ORPHAN_GRACE_SECONDS,
) -> list[str]:
    """重新投递"卡住"的 run（RETRYING/QUEUED 且 next_retry_at 已到）。

    为什么需要（**这不是 defense in depth，它是唯一的自动恢复路径**）：实测
    （Celery 5.6.3 + 真实 Redis broker + 真实 worker）普通 Exception、
    ``RetryPublicationError``、软/硬时间上限全部落在 ``Request.on_failure`` 的
    ``reject(requeue=False)`` 分支——**消息被丢弃、不重新投递**。只有
    ``WorkerLostError``（worker 进程被杀）才触发重投。因此：

    - 「让异常逃逸以触发 broker 重新投递」这个前提**是错的**，不成立；
    - run 停在 ``RETRYING``/``QUEUED`` 时，队列里**可能一条消息都没有**；
    - 消息本身随时可能丢（broker 重启、队列被清理、进程在 publish 前被杀）。

    所以恢复闭环 = 数据库里的 AgentRun 状态（``next_retry_at`` / ``attempt``）
    + **本扫描器被周期调度**（``runtime/celery_app.py::beat_schedule`` 驱动
    ``runtime.reconcile_stuck_runs``）。本函数未被调度 = 没有恢复闭环。
    ``scripts/reconcile_stuck_runs.py`` 只是同一逻辑的手动入口。

    另外两类也在这里收敛：

    1. **lease 已过期的 RUNNING**（``reclaim_stale_running=True``）——worker 执行途中
       lease 失效（心跳被瞬时 DB 错误打断、事件循环饿死、GC 停顿）时，它跑完图后
       完成提交会被 owner CAS 正确拒绝，任务正常返回被 ACK。此时 broker 不会再投递，
       而 ``list_recoverable_runs`` 也不看 RUNNING：**没有任何其它机制会再碰它**，
       run 会永久停在 RUNNING。这里重新投递后，新 worker 通过
       ``takeover_running``（同一条 UPDATE 内判定，仍然过期才接管）原子接管，
       attempt+1，并从 checkpoint 续跑。
    2. **预算已耗尽的过期 RUNNING** —— 直接落 DEAD_LETTER，避免「每次接管都再丢一次
       lease」形成无界热循环。DLQ 记录可观测，且可用
       ``scripts/replay_dead_run.py`` 人工重放（复用原 run_id，不绕过工具幂等键）。

    第三类：``PENDING``（``reclaim_stale_pending=True``）—— 两阶段入队
    （``create_run(status=PENDING)`` -> ``dispatch_run`` -> ``mark_queued``）在
    publish 前后被打断时留下的 run。恢复**不是**无脑重投：先做条件迁移
    ``PENDING -> QUEUED``（``promote_pending_to_queued``），由数据库裁决谁有权发
    这条消息，迁移失败即不发。宽限期（``pending_grace_seconds``）避免对「消息正在
    投递途中」的 run 立刻补投。

    刻意**不**回收 ``WAITING_APPROVAL``
    ------------------------------------
    等待人工审批的 run 不能自动重发：claim（审批已决策）之后 worker 崩溃的窗口里，
    审批载荷可能已经被消费但副作用尚未提交，自动重发无法证明「不会重复执行副作用」。
    保留人工 Runbook + 审批 TTL 到期按拒绝处理的既有安全边界。

    幂等性：重新投递一个已在执行的 run 不会重复执行——``mark_running`` 的 lease
    谓词在同一条 UPDATE 内判定（见 ``RunService.mark_running``）。同时
    **重复投递不消耗预算**：``attempt + 1`` 只发生在赢得 CAS 的那条 UPDATE 里。

    返回被重新投递的 run_id 列表（含被落 DLQ 的：两者都是「改变了状态」）。
    """
    now = now or datetime.now(timezone.utc)
    dispatched: list[str] = []
    candidates = service.list_recoverable_runs(limit=limit, now=now)
    for run_id in candidates:
        if dispatcher is None:
            from .dispatch import dispatch_run

            dispatcher = dispatch_run
        try:
            await dispatcher(run_id)
            dispatched.append(run_id)
        except Exception:
            # 单个投递失败不阻断其余 run；下一轮扫描会再试。
            continue

    if reclaim_stale_pending:
        for run_id in service.list_stale_pending_runs(
            limit=limit, now=now, grace_seconds=pending_grace_seconds
        ):
            # 条件迁移是「谁有权发这条消息」的裁决点：谓词不成立（已取消 /
            # 已被 mark_queued 处理 / 并发 reconciler 已抢先）就不发消息。
            if service.promote_pending_to_queued(run_id, now=now) is None:
                continue
            if dispatcher is None:
                from .dispatch import dispatch_run

                dispatcher = dispatch_run
            try:
                await dispatcher(run_id)
            except Exception:
                # 投递失败：run 停在 QUEUED + next_retry_at IS NULL，由
                # list_recoverable_runs 的 QUEUED 孤儿分支兜底重投。
                continue
            dispatched.append(run_id)

    if not reclaim_stale_running:
        return dispatched

    from . import metrics

    for row in service.list_stale_running_runs(limit=limit, now=now):
        run_id = row["id"]
        # 先无条件尝试「原子收敛 DLQ」：谓词（lease 仍过期 **且** 预算已耗尽）
        # 全在数据库层，与扫描快照无关。这是此前唯一按扫描快照在 Python 里分流
        # 「重投还是落 DLQ」的地方——扫描与写入之间的窗口里预算可能已被别的执行
        # 消耗掉，分流就会错。改成「先问数据库、答不上来再重投」之后，无论快照
        # 是否过时都不会做出错误的终态决定。
        try:
            updated = service.dead_letter_stale_running(
                run_id,
                error_code="stale_lease_attempts_exhausted",
                error_message=(
                    "RUNNING lease 已过期且 attempt 预算耗尽"
                    f"（{row['attempt']}/{row['max_attempts']}），"
                    "reconciler 拒绝无界重投"
                ),
                now=now,
            )
        except Exception:
            continue
        if updated is not None:
            metrics.record_stale_run_dead_lettered()
            dispatched.append(run_id)
            continue
        # 未收敛 = 预算未耗尽（可以再执行一次）或 已被接管 / 已续租 / 已终态。
        # 后几种情况重投是多余的，但无害：worker 侧的 lease 谓词会拒绝领取。
        if dispatcher is None:
            from .dispatch import dispatch_run

            dispatcher = dispatch_run
        try:
            await dispatcher(run_id)
        except Exception:
            continue
        metrics.record_stale_run_reclaimed()
        dispatched.append(run_id)
    return dispatched
