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
        result: dict[str, Any] = await graph.ainvoke(state, config=config)
        return result


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
]
