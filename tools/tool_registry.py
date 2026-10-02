"""
工具注册与执行框架（v3.5）
支持 OpenAI Function Calling 格式的工具定义、注册和执行。
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.logger import get_logger
from core.tool_result_cache import ToolCachePolicy

logger = get_logger("tools.registry")


@dataclass
class ToolDefinition:
    """工具定义（JSON Schema 格式，兼容 OpenAI Function Calling）"""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[..., Any]  # async callable(arguments: dict) -> str
    cache_policy: ToolCachePolicy = ToolCachePolicy()
    side_effect: bool = False
    """True = 写操作工具（退款/改单/建工单/发消息/ERP 写）。

    声明为副作用的工具在**异步 Run 执行上下文**内会被 ``runtime.side_effects``
    的 ledger 包裹：同一 ``(tool_name, run_id, tool_call_id)`` 一旦成功，重投递 /
    worker 崩溃恢复后直接返回已存结果，绝不重复触发外部副作用（at-least-once
    delivery + idempotent side effects）。

    只读工具保持 False：不落 ledger，也不承担重复执行风险。
    """
    risk_level: str | None = None
    """显式风险等级（low / medium / high），供 human-in-the-loop 审批闸门使用。

    None 表示「未声明」，此时由 ``core.hitl.risk.classify_risk`` 按工具名白名单 +
    金额阈值推断。显式声明优先，且优先级高于白名单——这样单个工具的风险语义
    写在工具定义处，而不是散落在环境变量里。

    high 的语义是「必须人工审批」；high 且 ``side_effect=True`` 才是完整形态
    （审批防不该做的被做，ledger 防做了被重做）。只读工具标 high 不会造成损害
    （闸门只拦 pending_actions，不拦只读工具的执行）。
    """


class ToolRegistry:
    """
    工具注册中心
    管理所有可用工具，提供 OpenAI tools 格式输出和执行调度。
    """

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Any],
        cache_policy: ToolCachePolicy | None = None,
        side_effect: bool = False,
        risk_level: str | None = None,
    ):
        """注册一个工具"""
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            cache_policy=cache_policy or ToolCachePolicy(),
            side_effect=side_effect,
            risk_level=risk_level,
        )
        logger.debug(f"工具已注册: {name}")

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """返回 OpenAI Function Calling 格式的工具列表"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(
        self, name: str, arguments: dict[str, Any],
        stream_callback: Callable | None = None,  # v6.0: 转发给工具 handler
        tool_call_id: str | None = None,
    ) -> str:
        """执行指定工具，返回字符串结果"""
        result = await self.execute_raw(
            name, arguments, stream_callback=stream_callback, tool_call_id=tool_call_id
        )
        return str(result) if result is not None else "查询完成，无结果"

    async def execute_raw(
        self,
        name: str,
        arguments: dict[str, Any],
        stream_callback: Callable | None = None,
        tool_call_id: str | None = None,
    ) -> Any:
        """执行工具并保留结构化返回值，供 Context Engineering 使用。"""
        tool = self._tools.get(name)
        if not tool:
            return f"错误：工具 '{name}' 不存在"

        idempotent_op = self._idempotent_operation(
            tool, arguments, tool_call_id, stream_callback=stream_callback
        )
        if idempotent_op is not None:
            # 副作用工具的失败必须冒泡：吞掉异常会把「写操作失败」伪装成成功 run，
            # 让上层 retry/DLQ 完全失效。
            return await idempotent_op()

        try:
            # v6.0: 注入 stream_callback，仅当 handler 接受此参数时传递
            if stream_callback is not None:
                sig = inspect.signature(tool.handler)
                if "stream_callback" in sig.parameters:
                    result = await tool.handler(arguments, stream_callback=stream_callback)
                else:
                    result = await tool.handler(arguments)
            else:
                result = await tool.handler(arguments)
            return result if result is not None else "查询完成，无结果"
        except Exception as e:
            logger.error(f"工具执行失败 [{name}]: {e}", exc_info=True)
            return f"工具 '{name}' 执行失败，请稍后重试"

    def _idempotent_operation(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str | None,
        *,
        stream_callback: Callable | None = None,
    ):
        """为副作用工具构造幂等执行闭包；不满足条件时返回 None（直接执行）。

        需要同时具备：
          - 工具声明 ``side_effect=True``；
          - 处于异步 Run 执行上下文（``run_id`` 来自 contextvar，worker 路径注入）；
          - 有稳定的 ``tool_call_id``（LLM Function Calling 的 tool_call id）。

        快路径（``/api/chat`` 等进程内同步执行）没有 run 上下文，返回 None，
        行为与改造前完全一致。
        """
        if not tool.side_effect or not tool_call_id:
            return None
        try:
            from runtime.context import get_current_run_id, get_current_thread_id
        except Exception:
            return None
        run_id = get_current_run_id()
        if not run_id:
            return None
        try:
            from runtime.side_effects import (
                build_tool_idempotency_key,
                execute_idempotent_operation,
            )
        except Exception:  # pragma: no cover - ledger 模块不可用时不做幂等包装
            logger.warning("side-effect ledger 不可用，工具将以非幂等方式执行: %s", tool.name)
            return None

        operation_key = build_tool_idempotency_key(run_id, tool_call_id)
        thread_id = get_current_thread_id()

        async def _run():
            logger.info(
                "副作用工具走幂等 ledger tool=%s run_id=%s op=%s",
                tool.name,
                run_id,
                operation_key,
            )
            return await execute_idempotent_operation(
                tool_name=tool.name,
                operation_key=operation_key,
                run_id=run_id,
                thread_id=thread_id,
                arguments=arguments,
                operation=lambda: self._call_handler(tool, arguments, stream_callback),
            )

        return _run

    @staticmethod
    async def _call_handler(
        tool: ToolDefinition, arguments: dict[str, Any], stream_callback: Callable | None
    ) -> Any:
        if stream_callback is not None:
            sig = inspect.signature(tool.handler)
            if "stream_callback" in sig.parameters:
                return await tool.handler(arguments, stream_callback=stream_callback)
            return await tool.handler(arguments)
        return await tool.handler(arguments)

    def list_tools(self) -> list[str]:
        """返回所有已注册工具名称"""
        return list(self._tools.keys())

    def cache_policy_for(self, name: str) -> ToolCachePolicy:
        """Return an explicit policy; unknown tools fail closed."""
        tool = self._tools.get(name)
        return tool.cache_policy if tool else ToolCachePolicy()

    def is_side_effect(self, name: str) -> bool:
        """该工具是否声明为写操作（需要幂等 ledger 保护）。"""
        tool = self._tools.get(name)
        return bool(tool.side_effect) if tool else False

    def risk_level_for(self, name: str) -> str | None:
        """该工具显式声明的风险等级；未声明/不存在返回 None。

        None 是有意义的「不知道」而不是「低风险」：调用方据此回退到
        ``core.hitl.risk.classify_risk`` 的白名单 + 金额阈值推断。未知工具返回
        None 而非 low，避免「查不到定义就当安全」的 fail-open。
        """
        tool = self._tools.get(name)
        return tool.risk_level if tool else None
