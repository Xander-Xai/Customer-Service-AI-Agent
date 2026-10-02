"""真实 LangGraph + 真实 PostgreSQL checkpointer 的 crash-recovery provider。

注入 Celery worker::

    AGENT_RUN_RUNTIME_PROVIDER=tests.integration.checkpoint_resume_provider:provide

与 ``fake_runtime_provider`` 的区别（这是 Gate 7 的核心）：
  - fake provider 不碰 checkpointer，只能证明「重投递后重跑」；
  - 本 provider 跑**真实 StateGraph**，用 ``core.checkpointer`` 建**真实
    AsyncPostgresSaver**，每个 super-step 都落 checkpoint。

图形状::

    START -> first  (快，记账后立即返回)
          -> second (先记账，再长时间阻塞)

worker 被 SIGKILL 时 ``second`` 尚未完成，其 super-step 没有 checkpoint；
恢复时 ``first`` 的 super-step **已有** checkpoint，必须**不被重新执行**。

证据（测试进程从 Redis 读）：
  - ``ckpt:node_first``   -> 期望恰好 1（证明不是从头重跑）
  - ``ckpt:node_second``  -> 期望 >=2（崩溃后重做未完成的那一步）
  - ``ckpt:trace``        -> 执行顺序日志
  - ``ckpt:recovered``    -> 检测到未完成 checkpoint 而续跑的次数
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, TypedDict

_FIRST_KEY = "ckpt:node_first"
_SECOND_KEY = "ckpt:node_second"
_TRACE_KEY = "ckpt:trace"
_RECOVERED_KEY = "ckpt:recovered"


class _ResumeState(TypedDict, total=False):
    customer_query: str
    response: str
    stage: str
    resumed: bool


def _redis_client():
    import redis

    url = os.getenv("REDIS_URL", "redis://localhost:6379")
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=5)


def _note(client: Any, node: str) -> int:
    key = _FIRST_KEY if node == "first" else _SECOND_KEY
    n = int(client.incr(key))
    client.rpush(_TRACE_KEY, f"{node}:{n}")
    return n


class _CheckpointResumeRuntime:
    """持有已编译的真实图（含 PG checkpointer）。"""

    def __init__(self, graph: Any, block_seconds: float):
        self._graph = graph
        self._block_seconds = block_seconds

    async def run(
        self,
        *,
        thread_id: str,
        query: str,
        user_id: str | None = None,
        multimodal_content: list | None = None,
    ) -> dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id}}

        # 复用**生产代码**的续跑判定与调用方式（runtime.bootstrap），而不是在测试
        # 里重写一份 —— 否则测试验证的是测试自己的逻辑，测不到真实缺陷。
        from runtime.bootstrap import invoke_graph_with_resume

        pending = await self._graph.aget_state(config)
        pending_steps = tuple(getattr(pending, "next", ()) or ())
        if pending_steps:
            try:
                client = _redis_client()
                client.incr(_RECOVERED_KEY)
                client.rpush(_TRACE_KEY, f"resume-from:{pending_steps}")
                client.close()
            except Exception:
                pass

        state: dict[str, Any] = {
            "customer_query": query,
            "response": "",
            "stage": "",
            "resumed": bool(pending_steps),
        }
        result, resumed = await invoke_graph_with_resume(self._graph, state, config)
        return {
            "response": result.get("response", ""),
            "stage": result.get("stage", ""),
            "resumed": resumed,
            "pending_on_entry": list(pending_steps),
            "thread_id": thread_id,
        }


_runtime: _CheckpointResumeRuntime | None = None


async def provide() -> _CheckpointResumeRuntime:
    """worker 注入点：构造真实图 + PG checkpointer（进程内单例）。"""
    global _runtime
    if _runtime is not None:
        return _runtime

    from langgraph.graph import END, START, StateGraph

    from core.checkpointer import build_postgres_checkpointer

    db_url = os.getenv("LANGGRAPH_CHECKPOINT_DATABASE_URL", "").strip() or os.getenv(
        "DATABASE_URL", ""
    )
    if not db_url:
        raise RuntimeError("checkpoint_resume_provider 需要 LANGGRAPH_CHECKPOINT_DATABASE_URL")

    rt = await build_postgres_checkpointer(db_url, min_size=1, max_size=3)
    if rt.backend != "postgres":
        raise RuntimeError(
            f"checkpoint_resume_provider 必须使用 postgres checkpointer，实际 {rt.backend}"
        )

    block_seconds = float(os.getenv("CKPT_BLOCK_SECONDS", "20"))
    client = _redis_client()

    async def first(state: _ResumeState) -> dict[str, Any]:
        _note(client, "first")
        # 返回后 LangGraph 为 first 的 super-step 写 checkpoint；
        # 崩溃恢复时本节点不得再执行。
        return {"response": "first-done", "stage": "first"}

    async def second(state: _ResumeState) -> dict[str, Any]:
        _note(client, "second")
        await asyncio.sleep(block_seconds)
        return {"response": "second-done", "stage": "second"}

    graph = (
        StateGraph(_ResumeState)
        .add_node("first", first)
        .add_node("second", second)
        .add_edge(START, "first")
        .add_edge("first", "second")
        .add_edge("second", END)
        .compile(checkpointer=rt.checkpointer)
    )

    _runtime = _CheckpointResumeRuntime(graph, block_seconds)
    return _runtime
