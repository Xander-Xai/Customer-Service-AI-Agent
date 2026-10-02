"""LangGraph Checkpoint 后端生命周期管理。

区分四个概念（不要混为一谈）：
  - Session Memory：``core/session/session_manager.py`` 的滑动窗口/摘要，服务端
    对话历史，键为 session_id。
  - LangGraph Checkpoint：本模块管理的图状态快照（thread_id == session_id），
    支持断点续传；生产使用官方 ``langgraph-checkpoint-postgres``。
  - Response Cache / Tool Result Store：分别见 ``cache/`` 与 ``core/tool_result_*.py``，
    与图状态持久化无关。

设计约束：
  - 官方 saver 管理自己的 checkpoint 表（``checkpoints`` / ``checkpoint_blobs`` /
    ``checkpoint_writes`` / ``checkpoint_migrations``），不耦合业务 SQLAlchemy Base。
  - 生产初始化失败必须 fail closed，绝不静默回退 MemorySaver；
    开发/测试允许显式使用 MemorySaver，且 postgres 不可用时降级会显式记录
    (status=degraded) 并出现在健康检查里。
  - 所有日志/健康输出不得包含数据库 URI、用户名或密码。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from core.config import (
    LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE,
    LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE,
    LANGGRAPH_CHECKPOINT_SETUP_TIMEOUT,
)
from core.logger import get_logger

logger = get_logger("core.checkpointer")

BACKEND_MEMORY = "memory"
BACKEND_POSTGRES = "postgres"

STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"


class CheckpointBackendError(RuntimeError):
    """checkpoint 后端创建/初始化失败。"""


def _record_checkpoint_error() -> None:
    """递增 checkpoint_errors_total（monitoring 不可用时静默）。"""
    try:
        from core import monitoring

        monitoring.checkpoint_errors_total.inc()
    except Exception:
        pass


@dataclass
class CheckpointRuntime:
    """一个已就绪的 checkpointer 运行时及其可选连接池。"""

    backend: str
    checkpointer: Any
    status: str = STATUS_HEALTHY
    detail: str = ""
    pool: Any = field(default=None, repr=False)

    def health(self) -> dict[str, str]:
        """返回脱敏健康信息（backend + status + 短 error 类型）。"""
        info = {"backend": self.backend, "status": self.status}
        if self.detail:
            info["detail"] = self.detail
        return info


def build_memory_checkpointer() -> CheckpointRuntime:
    """创建进程内 MemorySaver（仅供开发/测试）。"""
    try:
        from langgraph.checkpoint.memory import MemorySaver

        saver = MemorySaver()
    except ImportError as e:  # pragma: no cover - langgraph 是核心依赖
        raise CheckpointBackendError(
            "langgraph.checkpoint.memory 不可用，无法创建 MemorySaver"
        ) from e
    return CheckpointRuntime(backend=BACKEND_MEMORY, checkpointer=saver)


async def build_postgres_checkpointer(
    database_url: str | None,
    *,
    min_size: int = LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE,
    max_size: int = LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE,
    setup_timeout: float = LANGGRAPH_CHECKPOINT_SETUP_TIMEOUT,
) -> CheckpointRuntime:
    """创建官方 AsyncPostgresSaver（psycopg 异步连接池）。

    连接池 open + 官方 ``setup()`` 建表在初始化阶段完成；任何失败都关闭连接池并
    抛 ``CheckpointBackendError``，由调用方决定 fail closed 或（仅开发）显式降级。
    """
    if not database_url:
        raise CheckpointBackendError(
            "postgres checkpoint backend requires a PostgreSQL database URL"
        )
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
    except ImportError as e:
        raise CheckpointBackendError(
            "langgraph-checkpoint-postgres / psycopg[binary] 未安装，"
            "无法启用 postgres checkpoint 后端"
        ) from e

    # 先做一次直接连接预检：连接失败时不创建连接池，避免遗留 pool worker。
    connect_timeout = max(1, int(setup_timeout))
    try:
        probe = await AsyncConnection.connect(
            database_url, autocommit=True, connect_timeout=connect_timeout
        )
    except Exception as e:
        _record_checkpoint_error()
        raise CheckpointBackendError(
            f"PostgreSQL checkpoint 连接失败（{type(e).__name__}）"
        ) from e
    await probe.close()

    pool = AsyncConnectionPool(
        conninfo=database_url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    try:
        await pool.open(wait=True, timeout=setup_timeout)
        saver = AsyncPostgresSaver(pool)
        # 官方推荐：首次使用时调用 setup() 创建/迁移 checkpoint 表。
        await saver.setup()
    except Exception as e:
        _record_checkpoint_error()
        await _close_pool(pool)
        raise CheckpointBackendError(
            f"PostgreSQL checkpoint 初始化失败（{type(e).__name__}）"
        ) from e

    logger.info(
        "LangGraph checkpoint 后端已就绪: postgres (pool %s-%s)", min_size, max_size
    )
    return CheckpointRuntime(
        backend=BACKEND_POSTGRES, checkpointer=saver, pool=pool
    )


async def _close_pool(pool: Any) -> None:
    if pool is None:
        return
    try:
        close = getattr(pool, "close", None)
        if close is None:
            return
        try:
            result = close(timeout=2.0)
        except TypeError:
            result = close()
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:  # pragma: no cover - close 尽力而为
        logger.warning("checkpoint 连接池关闭异常: %s", type(e).__name__)


async def close_checkpoint_runtime(runtime: CheckpointRuntime | None) -> None:
    """释放 checkpoint 后端资源（幂等）。"""
    if runtime is None:
        return
    await _close_pool(runtime.pool)


async def probe_checkpoint_runtime(
    runtime: CheckpointRuntime | None, *, timeout: float = 3.0
) -> dict[str, str]:
    """探活 checkpoint 后端，返回脱敏 ``{backend, status}``。"""
    if runtime is None:
        return {"backend": "disabled", "status": STATUS_UNAVAILABLE}
    if runtime.backend == BACKEND_MEMORY:
        return runtime.health()
    pool = runtime.pool
    if pool is None:
        return {"backend": runtime.backend, "status": STATUS_UNAVAILABLE}
    try:
        async with pool.connection(timeout=timeout) as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1")
        return {"backend": runtime.backend, "status": STATUS_HEALTHY}
    except Exception as e:
        logger.debug("checkpoint 探活失败: %s", type(e).__name__)
        return {
            "backend": runtime.backend,
            "status": STATUS_UNAVAILABLE,
            "detail": type(e).__name__,
        }
