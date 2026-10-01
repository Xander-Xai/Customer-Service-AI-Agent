"""Run 投递：把 run_id 发送到 Celery（生产）或进程内执行（开发 fallback）。

队列消息只携带 ``run_id``；完整 query/state 由 worker 从 PostgreSQL 读取，
避免把 prompt/state 放进 Celery payload。
"""

from __future__ import annotations

from core.config import AGENT_RUN_DISPATCH, AGENT_RUN_QUEUE
from core.logger import get_logger

logger = get_logger("runtime.dispatch")


async def dispatch_run(run_id: str, countdown: float | None = None) -> str:
    """投递 run，返回实际使用的 dispatch 模式。

    ``countdown`` 用于延迟重调度（thread 竞争 / 指数退避），避免 busy-loop。
    """
    mode = (AGENT_RUN_DISPATCH or "celery").strip().lower()
    if mode == "celery":
        from .tasks import execute_agent_run

        execute_agent_run.apply_async(
            args=[run_id], queue=AGENT_RUN_QUEUE, countdown=countdown or 0
        )
        return "celery"
    if mode == "inline":
        # 开发/测试 fallback：在 API 进程内后台执行（失去进程解耦，不用于生产）
        import asyncio

        from .executor import execute_run

        async def _delayed() -> None:
            if countdown and countdown > 0:
                await asyncio.sleep(countdown)
            await execute_run(run_id)

        asyncio.create_task(_delayed())
        logger.warning("AGENT_RUN_DISPATCH=inline：run 在 API 进程内执行（非生产路径）")
        return "inline"
    raise RuntimeError(f"未知 AGENT_RUN_DISPATCH: {mode!r}（仅支持 celery | inline）")
