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
    # 写操作工具：执行前经 SideEffect 幂等保护（at-least-once + app idempotency）
    side_effect: bool = False
    # 业务操作键构造器（例如 user/order/refund-action）；为空则按参数哈希兜底
    operation_key: Callable[[dict[str, Any]], str] | None = None
    # Human-in-the-loop 风险等级：low / medium / high（high 需人工审批）
    risk_level: str = "low"


class ToolRegistry:
    """
    工具注册中心
    管理所有可用工具，提供 OpenAI tools 格式输出和执行调度。
    """

    def __init__(self, side_effect_store: Any = None):
        self._tools: dict[str, ToolDefinition] = {}
        self._side_effect_store = side_effect_store

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Any],
        cache_policy: ToolCachePolicy | None = None,
        side_effect: bool = False,
        operation_key: Callable[[dict[str, Any]], str] | None = None,
        risk_level: str = "low",
    ):
        """注册一个工具。``side_effect=True`` 的写操作工具受幂等保护。"""
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            cache_policy=cache_policy or ToolCachePolicy(),
            side_effect=side_effect,
            operation_key=operation_key,
            risk_level=risk_level,
        )
        logger.debug(f"工具已注册: {name} (side_effect={side_effect}, risk={risk_level})")

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
    ) -> str:
        """执行指定工具，返回字符串结果"""
        result = await self.execute_raw(name, arguments, stream_callback=stream_callback)
        return str(result) if result is not None else "查询完成，无结果"

    async def execute_raw(
        self, name: str, arguments: dict[str, Any], stream_callback: Callable | None = None
    ) -> Any:
        """执行工具并保留结构化返回值，供 Context Engineering 使用。"""
        tool = self._tools.get(name)
        if not tool:
            return f"错误：工具 '{name}' 不存在"
        if tool.side_effect:
            return await self._execute_side_effect(tool, arguments, stream_callback)
        try:
            return await self._invoke_handler(tool, arguments, stream_callback)
        except Exception as e:
            logger.error(f"工具执行失败 [{name}]: {e}", exc_info=True)
            return f"工具 '{name}' 执行失败，请稍后重试"

    async def _invoke_handler(
        self, tool: ToolDefinition, arguments: dict[str, Any], stream_callback: Callable | None
    ) -> Any:
        """调用工具 handler（按签名决定是否传 stream_callback）。"""
        if stream_callback is not None:
            sig = inspect.signature(tool.handler)
            if "stream_callback" in sig.parameters:
                result = await tool.handler(arguments, stream_callback=stream_callback)
            else:
                result = await tool.handler(arguments)
        else:
            result = await tool.handler(arguments)
        return result if result is not None else "查询完成，无结果"

    async def _execute_side_effect(
        self, tool: ToolDefinition, arguments: dict[str, Any], stream_callback: Callable | None
    ) -> Any:
        """写操作工具：先查/声明幂等记录，成功结果落库后才返回。

        注意：这是 application-level idempotency，不是端到端 exactly-once。
        远程副作用成功与本地记录提交之间仍存在极小窗口。
        """
        from runtime.context import get_current_run_id, get_current_thread_id
        from runtime.errors import PermanentError, classify_exception, safe_error_message
        from runtime.side_effects import (
            CLAIM_CONFLICT,
            CLAIM_SUCCEEDED,
            default_operation_key,
            get_side_effect_store,
            request_fingerprint,
        )

        run_id = get_current_run_id()
        if not run_id:
            # 无 run 上下文无法保证幂等 -> fail closed（不执行写操作）
            raise PermanentError(
                f"side-effect tool '{tool.name}' requires run context (run_id)"
            )

        store = self._side_effect_store or get_side_effect_store()
        op_key = (
            tool.operation_key(arguments)
            if tool.operation_key
            else default_operation_key(tool.name, arguments)
        )
        fingerprint = request_fingerprint(arguments)

        claim = store.claim(
            tool_name=tool.name,
            operation_key=op_key,
            run_id=run_id,
            thread_id=get_current_thread_id(),
            fingerprint=fingerprint,
        )
        if claim.state == CLAIM_SUCCEEDED:
            self._record_idempotency_hit(tool.name)
            return claim.result
        if claim.state == CLAIM_CONFLICT:
            raise PermanentError(
                f"side-effect conflict: {tool.name} operation_key={op_key} "
                "已存在且请求指纹不同"
            )

        try:
            result = await self._invoke_handler(tool, arguments, stream_callback)
        except Exception as e:
            store.mark_failed(
                tool.name,
                op_key,
                error_type=classify_exception(e),
                error_message=safe_error_message(e),
            )
            raise
        store.mark_succeeded(tool.name, op_key, result)
        return result

    @staticmethod
    def _record_idempotency_hit(tool_name: str) -> None:
        try:
            from core.monitoring import tool_idempotency_hit_total

            tool_idempotency_hit_total.inc()
        except Exception:
            pass

    def list_tools(self) -> list[str]:
        """返回所有已注册工具名称"""
        return list(self._tools.keys())

    def cache_policy_for(self, name: str) -> ToolCachePolicy:
        """Return an explicit policy; unknown tools fail closed."""
        tool = self._tools.get(name)
        return tool.cache_policy if tool else ToolCachePolicy()

    def risk_level_for(self, name: str) -> str | None:
        """Return the declared risk level, or None when unknown."""
        tool = self._tools.get(name)
        return tool.risk_level if tool else None
