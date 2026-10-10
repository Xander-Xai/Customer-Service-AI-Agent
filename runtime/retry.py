"""重试退避策略：指数退避 + 抖动，硬上限。

禁止无限 autoretry：attempt 由 ``AGENT_RUN_MAX_ATTEMPTS`` 约束。
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any


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
) -> list[str]:
    """重新投递"卡住"的 run（RETRYING/QUEUED 且 next_retry_at 已到）。

    为什么需要（defense in depth）：主路径已经在投递失败时让异常逃逸，靠 Celery 的
    ``acks_late`` 重新投递来恢复。但 still存在 at-least-once 之外的情形让消息彻底消失
    （broker 重启丢队列、消息被手工清理、inline 模式下进程内存任务丢失）。此时
    ``next_retry_at`` 已经过期却没有任何调度器，这个扫描器就是那个调度器。

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

    刻意**不**回收 ``WAITING_APPROVAL``
    ------------------------------------
    等待人工审批的 run 不能自动重发：claim（审批已决策）之后 worker 崩溃的窗口里，
    审批载荷可能已经被消费但副作用尚未提交，自动重发无法证明「不会重复执行副作用」。
    保留人工 Runbook + 审批 TTL 到期按拒绝处理的既有安全边界。

    幂等性：重新投递一个已在执行的 run 不会重复执行——``mark_running`` 会因为
    其它 worker 的有效 lease 返回 None（见 ``RunService.mark_running``）。

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

    if not reclaim_stale_running:
        return dispatched

    from . import metrics

    for row in service.list_stale_running_runs(limit=limit, now=now):
        run_id = row["id"]
        if int(row["attempt"]) >= int(row["max_attempts"]):
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
            if updated is None:
                # 谓词不成立 = 已被接走 / 已续租 / 已终态。benign no-op。
                continue
            metrics.record_stale_run_dead_lettered()
            dispatched.append(run_id)
            continue
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
