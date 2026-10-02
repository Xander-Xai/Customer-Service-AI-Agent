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
) -> list[str]:
    """重新投递"卡住"的 run（RETRYING/QUEUED 且 next_retry_at 已到）。

    为什么需要（defense in depth）：主路径已经在投递失败时让异常逃逸，靠 Celery 的
    ``acks_late`` 重新投递来恢复。但 still存在 at-least-once 之外的情形让消息彻底消失
    （broker 重启丢队列、消息被手工清理、inline 模式下进程内存任务丢失）。此时
    ``next_retry_at`` 已经过期却没有任何调度器，这个扫描器就是那个调度器。

    幂等性：重新投递一个已在执行的 run 不会重复执行——``mark_running`` 会因为
    其它 worker 的有效 lease 返回 None（见 ``RunService.mark_running``）。

    返回被重新投递的 run_id 列表。
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
    return dispatched
