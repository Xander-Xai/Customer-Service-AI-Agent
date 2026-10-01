"""分布式 runtime 故障注入 / 恢复测试 harness。

设计原则：
  - 不在生产代码里散落 ``if TEST``；通过依赖注入（runtime provider / lock /
    stream / side-effect store）与 fake tool 实现可控故障。
  - 复用真实组件：``execute_run`` / ``RunService`` / ``SideEffectStore`` /
    ``ToolRegistry`` / LangGraph checkpointer。
  - SQLite 独立引擎 + 进程内 lock/stream，不依赖外部服务即可跑默认用例。
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base
from runtime.errors import TransientError
from runtime.event_stream import InMemoryRunEventStream
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.side_effects import SideEffectStore
from runtime.thread_lock import InMemoryThreadLock


class GraphState(TypedDict, total=False):
    nodes: list[str]
    refund: dict


def make_run_env():
    """独立 SQLite + RunService（每个测试隔离）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    service = RunService(AgentRunRepository(session_factory))
    return service, session_factory


def make_lock() -> InMemoryThreadLock:
    return InMemoryThreadLock()


def make_stream() -> InMemoryRunEventStream:
    return InMemoryRunEventStream()


def make_side_effect_store(session_factory) -> SideEffectStore:
    return SideEffectStore(session_factory)


async def noop_dispatcher(run_id: str, countdown: float | None = None) -> None:
    return None


def queued(service: RunService, *, thread: str = "T1", session: str = "s1", max_attempts: int = 3) -> str:
    run = service.create_run(
        query="q", session_id=session, thread_id=thread, max_attempts=max_attempts
    )
    service.mark_queued(run["id"])
    return run["id"]


def build_checkpointed_graph(
    checkpointer: Any,
    *,
    node_log: list[str],
    crash_flag: dict[str, bool],
    refund_registry: Any = None,
):
    """node1 -> node2 -> node3。

    - node2 在 ``crash_flag['crash']`` 为真时抛出 TransientError（模拟 worker 被杀）；
    - node2 可选调用 fake refund 工具（side_effect）。
    """
    from langgraph.graph import StateGraph

    async def node1(state: GraphState) -> GraphState:
        node_log.append("node1")
        return {"nodes": state.get("nodes", []) + ["node1"]}

    async def node2(state: GraphState) -> GraphState:
        node_log.append("node2")
        refund = None
        if refund_registry is not None:
            refund = await refund_registry.execute_raw("refund", {"order": "ORD-9"})
        if crash_flag.get("crash"):
            crash_flag["crash"] = False  # 只崩一次
            raise TransientError("worker terminated after node2")
        out: GraphState = {"nodes": state.get("nodes", []) + ["node2"]}
        if refund is not None:
            out["refund"] = refund if isinstance(refund, dict) else {"value": refund}
        return out

    async def node3(state: GraphState) -> GraphState:
        node_log.append("node3")
        return {"nodes": state.get("nodes", []) + ["node3"]}

    graph = StateGraph(GraphState)
    graph.add_node("node1", node1)
    graph.add_node("node2", node2)
    graph.add_node("node3", node3)
    graph.set_entry_point("node1")
    graph.add_edge("node1", "node2")
    graph.add_edge("node2", "node3")
    graph.set_finish_point("node3")
    return graph.compile(checkpointer=checkpointer)


class CheckpointedRuntime:
    """模拟 worker 的 graph 执行：有 checkpoint 时用 ``None`` resume。

    worker 崩溃后重启（新 graph 实例、共享 checkpointer）可从未完成的 checkpoint
    继续，避免重跑已完成节点。
    """

    def __init__(
        self,
        checkpointer: Any,
        *,
        node_log: list[str] | None = None,
        crash_flag: dict[str, bool] | None = None,
        refund_registry: Any = None,
    ):
        self.checkpointer = checkpointer
        self.node_log = node_log if node_log is not None else []
        self.crash_flag = crash_flag if crash_flag is not None else {"crash": False}
        self.refund_registry = refund_registry

    async def run(self, *, thread_id: str, query: str, user_id: str | None = None) -> dict[str, Any]:
        graph = build_checkpointed_graph(
            self.checkpointer,
            node_log=self.node_log,
            crash_flag=self.crash_flag,
            refund_registry=self.refund_registry,
        )
        config = {"configurable": {"thread_id": thread_id}}
        existing = await self.checkpointer.aget_tuple(config)
        if existing is not None:
            result = await graph.ainvoke(None, config=config)
        else:
            result = await graph.ainvoke({"nodes": []}, config=config)
        return {"response": "done", "nodes": result.get("nodes", [])}


class BlockingRuntime:
    """可阻塞的 runtime，用于并发/重启场景。"""

    def __init__(self):
        self.entered: dict[str, asyncio.Event] = {}
        self.release = asyncio.Event()
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0

    def event_for(self, thread_id: str) -> asyncio.Event:
        return self.entered.setdefault(thread_id, asyncio.Event())

    async def wait_entered(self, thread_id: str, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.event_for(thread_id).wait(), timeout=timeout)

    async def run(self, *, thread_id: str, query: str, user_id: str | None = None) -> dict[str, Any]:
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.event_for(thread_id).set()
        try:
            await self.release.wait()
            return {"response": "ok", "thread_id": thread_id}
        finally:
            self.concurrent -= 1


class FailingRuntime:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    async def run(self, *, thread_id: str, query: str, user_id: str | None = None) -> dict[str, Any]:
        self.calls += 1
        raise self.exc


class FakeRefundTool:
    """记录调用次数的 fake refund tool（side_effect=True）。"""

    def __init__(self, session_factory):
        from tools.tool_registry import ToolRegistry

        self.calls = 0
        self.store = SideEffectStore(session_factory)
        self.registry = ToolRegistry(side_effect_store=self.store)
        self.registry.register(
            name="refund",
            description="refund an order",
            parameters={},
            handler=self._refund,
            side_effect=True,
            operation_key=lambda args: f"refund:{args.get('order')}",
        )

    async def _refund(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"refund_id": f"R-{self.calls}", "order": args.get("order")}
