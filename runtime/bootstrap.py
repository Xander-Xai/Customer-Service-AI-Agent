"""可复用 runtime bootstrap：API 与 Worker 使用同一套 ServiceContainer。

Worker 只调用 ``AgentRuntime``，不初始化 FastAPI server。API 侧的实时快路径
仍沿用 ``api/app.py::_run_graph``；本模块是异步 Run 路径（``/api/runs`` +
Celery worker）的执行入口，两者共享同一图与同一 checkpoint 后端。
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from core.logger import get_logger, get_trace_id, set_trace_id

logger = get_logger("runtime.bootstrap")


async def pending_steps(graph: Any, config: dict[str, Any]) -> tuple[str, ...]:
    """读取该 thread 最新 checkpoint 的待执行节点（无待执行 = 空 tuple）。

    语义：只有「上一次执行**没走完**」（next 非空）才算崩溃恢复。对话历史留存的
    已完成快照（next 为空）不算，那是正常多轮续聊。
    探活失败一律按「无待执行」处理，绝不让 checkpoint 查询异常阻断业务。
    """
    aget_state = getattr(graph, "aget_state", None)
    if aget_state is None:
        return ()
    try:
        snapshot = await aget_state(config)
    except Exception as e:  # pragma: no cover - 探活失败不阻断执行
        logger.debug("checkpoint resume 探测失败: %s", type(e).__name__)
        return ()
    if snapshot is None:
        return ()
    return tuple(getattr(snapshot, "next", ()) or ())


async def invoke_graph_with_resume(
    graph: Any, state: dict[str, Any] | None, config: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """执行图：存在未完成 checkpoint 时续跑，否则正常执行。返回 ``(result, resumed)``。

    **必须**区分两种调用方式（LangGraph 1.2.x 实测语义）::

        ainvoke(None, cfg)   -> 从 checkpoint 的 next 继续，不重跑已完成节点
        ainvoke(state, cfg)  -> 从 START 重新执行，并用入参覆盖 channel 值

    因此崩溃恢复若传 state 会退化成「从头重跑」：已完成的上游节点被重复执行，
    其副作用（工具调用、外部写操作）会被重复触发。worker 崩溃恢复的正确语义是
    ``ainvoke(None)``。

    同时上报 ``agent_checkpoint_recovery_total``。
    """
    pending = await pending_steps(graph, config)
    resumed = bool(pending)

    from . import metrics as run_metrics

    run_metrics.record_checkpoint_recovery(resumed)

    if resumed:
        logger.info(
            "从持久化 checkpoint 续跑（跳过已完成节点）thread=%s pending=%s",
            config.get("configurable", {}).get("thread_id"),
            pending,
        )
        result = await graph.ainvoke(None, config=config)
    else:
        result = await graph.ainvoke(state, config=config)
    return result, resumed


class AgentRuntime:
    """持有已初始化的 ServiceContainer，执行 LangGraph。"""

    def __init__(self, container: Any = None):
        self.container = container
        self._started = False

    async def start(self) -> AgentRuntime:
        if self._started:
            return self
        if self.container is None:
            from core.container import ServiceContainer

            self.container = ServiceContainer()
        await self.container.initialize()
        self._started = True
        logger.info("AgentRuntime 初始化完成（worker runtime）")
        return self

    async def stop(self) -> None:
        if self.container is not None:
            try:
                await self.container.close()
            except Exception as e:  # pragma: no cover - 关闭尽力而为
                logger.warning("AgentRuntime 关闭异常: %s", type(e).__name__)
        self._started = False

    async def run(
        self,
        *,
        thread_id: str,
        query: str,
        user_id: str | None = None,
        multimodal_content: list | None = None,
    ) -> dict[str, Any]:
        """执行图并返回结果。thread_id == session_id（与 API 语义一致）。"""
        if not self._started:
            await self.start()

        from core.shared_blackboard import set_blackboard_session_id

        set_blackboard_session_id(thread_id)

        state: dict[str, Any] = {
            "session_id": thread_id,
            "current_agent": "",
            "customer_query": query,
            "query_type": "",
            "response": "",
            "complexity": 0,
            "fast_path": True,
            "collaboration_mode": "",
            "cached": False,
            "agents_used": [],
            "resolution_status": "",
            "trace_id": get_trace_id(),
        }
        if user_id:
            state["user_id"] = user_id
        if multimodal_content:
            state["multimodal_content"] = multimodal_content
            state["has_multimodal"] = True

        trace_id = state["trace_id"]
        if trace_id:
            set_trace_id(trace_id)

        graph = self.container.graph_app
        config = {"configurable": {"thread_id": thread_id}}
        result, _resumed = await invoke_graph_with_resume(graph, state, config)
        return result

    @staticmethod
    async def _pending_steps(graph: Any, config: dict[str, Any]) -> tuple[str, ...]:
        return await pending_steps(graph, config)


_default_runtime: AgentRuntime | None = None


async def get_default_runtime() -> AgentRuntime:
    """返回进程内单例 runtime（惰性初始化）。"""
    global _default_runtime
    if _default_runtime is None or not _default_runtime._started:
        runtime = AgentRuntime()
        await runtime.start()
        _default_runtime = runtime
    return _default_runtime


async def shutdown_default_runtime() -> None:
    """关闭单例 runtime（worker 优雅退出）。"""
    global _default_runtime
    runtime = _default_runtime
    _default_runtime = None
    if runtime is not None:
        await runtime.stop()


def resolve_runtime_provider():
    """解析 worker 使用的 runtime provider。

    默认返回 :func:`get_default_runtime`（真实 ServiceContainer + LangGraph）。
    可通过 ``AGENT_RUN_RUNTIME_PROVIDER=module:callable`` 注入替身，用于无外部
    Provider 凭据的分布式基础设施测试（deterministic fake graph）。这是一个
    依赖注入接缝，不是伪造执行结果——被注入的 provider 仍走完整的 Run 状态机、
    thread lock、retry、DLQ 与 worker 崩溃恢复路径。
    """
    spec = os.getenv("AGENT_RUN_RUNTIME_PROVIDER", "").strip()
    if not spec:
        return get_default_runtime
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError("AGENT_RUN_RUNTIME_PROVIDER 格式应为 'module:callable'")
    module = importlib.import_module(module_name)
    provider = getattr(module, attr, None)
    if not callable(provider):
        raise ValueError(f"AGENT_RUN_RUNTIME_PROVIDER 不可调用: {spec!r}")
    return provider


def reset_default_runtime_for_tests() -> None:
    """仅测试使用：清空单例引用（不关闭）。"""
    global _default_runtime
    _default_runtime = None


__all__ = [
    "AgentRuntime",
    "get_default_runtime",
    "shutdown_default_runtime",
    "resolve_runtime_provider",
    "reset_default_runtime_for_tests",
    "pending_steps",
    "invoke_graph_with_resume",
]
