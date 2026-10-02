"""副作用工具 + worker 崩溃 的幂等验证 provider（Gate 11）。

注入 Celery worker::

    AGENT_RUN_RUNTIME_PROVIDER=tests.integration.side_effect_provider:provide

要点：
  - 用**真实** ``tools.ToolRegistry``，工具以 ``side_effect=True`` 注册，因此生产
    路径 ``ToolRegistry.execute_raw`` 会自动套上 ``runtime.side_effects`` ledger；
  - ledger 落到**真实 PostgreSQL**（``tool_side_effects`` 表）；
  - ``run_id`` 由 ``runtime.executor`` 注入 contextvar，走真实生产路径；
  - ``tool_call_id`` 由 thread_id 确定性派生 —— 崩溃重投后必须命中同一幂等键。

崩溃点必须落在「工具已成功、但图未完成」::

    START -> charge : 执行副作用工具(ledger) -> phase=1 -> 阻塞
          -> done

Worker A 在 ``charge`` 阻塞时被 SIGKILL：副作用已落到外部系统（counter=1，ledger
SUCCEEDED），但 ``charge`` 的 super-step 没有 checkpoint。

Worker B 从 checkpoint 续跑，重做 ``charge``：ledger 命中 SUCCEEDED -> 返回历史
结果，**不再执行副作用**（counter 保持 1），并计入
``agent_tool_idempotency_hit_total``。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, TypedDict

#: Redis key 前缀。默认 ``idem`` 保持既有行为；调用方（测试）用
#: ``SIDE_EFFECT_KEY_PREFIX`` 按 run 打标签。之前四个 key 全局固定，于是
#: 开头的 ``client.delete`` 清完之后，仍存活的 worker 进程可以继续改写同一组
#: key：``phase`` 被改写会让下面的命中统计与 ledger 事实脱钩。
KEY_PREFIX = os.getenv("SIDE_EFFECT_KEY_PREFIX", "idem").strip().rstrip(":") or "idem"


def _key(name: str) -> str:
    return f"{KEY_PREFIX}:{name}"


COUNTER_KEY = _key("counter")
HITS_KEY = _key("ledger_hits")
PHASE_KEY = _key("phase")
TOOL_CALLS_KEY = _key("tool_invocations")

TOOL_NAME = "increment_counter"
BLOCK_SECONDS = float(os.getenv("SIDE_EFFECT_BLOCK_SECONDS", "25"))


class _State(TypedDict, total=False):
    customer_query: str
    response: str
    stage: str


def _redis_client():
    import redis

    url = os.getenv("REDIS_URL", "redis://localhost:6379")
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=5)


def _build_registry():
    """真实 ToolRegistry，写工具以 ``side_effect=True`` 注册。"""
    from tools.tool_registry import ToolRegistry

    registry = ToolRegistry()

    async def increment_counter(arguments: dict[str, Any]) -> dict[str, Any]:
        """被保护的外部副作用：真实写 Redis 计数器（模拟退款/改单等写操作）。"""
        client = _redis_client()
        try:
            value = int(client.incr(COUNTER_KEY))
        finally:
            client.close()
        return {"counter": value, "amount": arguments.get("amount")}

    registry.register(
        name=TOOL_NAME,
        description="测试用副作用工具：把全局计数器 +1（模拟退款/改单等写操作）",
        parameters={
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "order": {"type": "string"},
            },
            "required": ["amount"],
        },
        handler=increment_counter,
        side_effect=True,
    )
    return registry


def _build_graph(thread_id: str, registry: Any, checkpointer: Any):
    """按 thread_id 编译图：tool_call_id 由 thread_id 确定性派生。"""
    from langgraph.graph import END, START, StateGraph

    async def charge(state: _State) -> dict[str, Any]:
        client = _redis_client()
        tool_call_id = f"{thread_id}:refund"
        client.incr(TOOL_CALLS_KEY)
        # 记账：本次调用前副作用计数器的值
        counter_before = int(client.get(COUNTER_KEY) or 0)
        client.close()

        # 真实 ToolRegistry 路径：side_effect=True -> 自动套 ledger 幂等
        result = await registry.execute_raw(
            TOOL_NAME,
            {"amount": 100, "order": "SO-1"},
            tool_call_id=tool_call_id,
        )

        client = _redis_client()
        # ledger 是否命中：**直接观测副作用计数器**，而不是用 phase 推断。
        # 命中 ledger 时 execute_raw 返回历史结果，handler 根本不会执行，
        # COUNTER_KEY 保持不变；真正执行则必然 +1。
        # 旧实现用 ``first_pass``（即 phase）决定是否记一次命中，而 phase 只是
        # 「本次是否阻塞」的标记：残留 worker / 并发 run 改写 phase 时，命中数会
        # 与 ledger 事实脱钩，既能让真失败通过，也能让真通过误报。
        counter_after = int(client.get(COUNTER_KEY) or 0)
        if counter_after == counter_before:
            client.incr(HITS_KEY)

        # phase 只负责「首次执行要阻塞」这个崩溃窗口，与 ledger 判定解耦。
        first_pass = client.get(PHASE_KEY) != "1"
        if first_pass:
            client.set(PHASE_KEY, "1")
        client.close()

        if first_pass:
            await asyncio.sleep(BLOCK_SECONDS)
        return {"response": str(result), "stage": "charged"}

    async def done(state: _State) -> dict[str, Any]:
        return {"stage": "done"}

    return (
        StateGraph(_State)
        .add_node("charge", charge)
        .add_node("done", done)
        .add_edge(START, "charge")
        .add_edge("charge", "done")
        .add_edge("done", END)
        .compile(checkpointer=checkpointer)
    )


class _SideEffectRuntime:
    def __init__(self, registry: Any, checkpointer: Any):
        self._registry = registry
        self._checkpointer = checkpointer

    async def run(
        self,
        *,
        thread_id: str,
        query: str,
        user_id: str | None = None,
        multimodal_content: list | None = None,
    ) -> dict[str, Any]:
        from runtime.bootstrap import invoke_graph_with_resume

        graph = _build_graph(thread_id, self._registry, self._checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        state: dict[str, Any] = {"customer_query": query, "response": "", "stage": ""}
        result, _resumed = await invoke_graph_with_resume(graph, state, config)
        return {
            "response": result.get("response", ""),
            "stage": result.get("stage", ""),
            "thread_id": thread_id,
        }


_runtime: _SideEffectRuntime | None = None


async def provide() -> _SideEffectRuntime:
    """worker 注入点：真实 ToolRegistry + 真实 PG checkpointer（进程内单例）。"""
    global _runtime
    if _runtime is not None:
        return _runtime

    from core.checkpointer import build_postgres_checkpointer

    db_url = os.getenv("LANGGRAPH_CHECKPOINT_DATABASE_URL", "").strip() or os.getenv(
        "DATABASE_URL", ""
    )
    if not db_url:
        raise RuntimeError("side_effect_provider 需要 PostgreSQL")

    rt = await build_postgres_checkpointer(db_url, min_size=1, max_size=3)
    if rt.backend != "postgres":
        raise RuntimeError(f"必须 postgres checkpointer，实际 {rt.backend}")

    _runtime = _SideEffectRuntime(_build_registry(), rt.checkpointer)
    return _runtime
