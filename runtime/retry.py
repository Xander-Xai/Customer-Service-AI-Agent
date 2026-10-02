"""重试退避策略：指数退避 + 抖动，硬上限。

禁止无限 autoretry：attempt 由 ``AGENT_RUN_MAX_ATTEMPTS`` 约束。
"""

from __future__ import annotations

import random
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
