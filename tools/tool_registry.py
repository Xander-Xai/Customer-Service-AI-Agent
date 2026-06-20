"""
工具注册与执行框架（v3.5）
支持 OpenAI Function Calling 格式的工具定义、注册和执行。
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.logger import get_logger

logger = get_logger("tools.registry")


@dataclass
class ToolDefinition:
    """工具定义（JSON Schema 格式，兼容 OpenAI Function Calling）"""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[..., Any]  # async callable(arguments: dict) -> str


class ToolRegistry:
    """
    工具注册中心
    管理所有可用工具，提供 OpenAI tools 格式输出和执行调度。
    """

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self, name: str, description: str, parameters: dict[str, Any], handler: Callable[..., Any]
    ):
        """注册一个工具"""
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
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
    ) -> str:
        """执行指定工具，返回字符串结果"""
        tool = self._tools.get(name)
        if not tool:
            return f"错误：工具 '{name}' 不存在"
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
            return str(result) if result is not None else "查询完成，无结果"
        except Exception as e:
            logger.error(f"工具执行失败 [{name}]: {e}", exc_info=True)
            return f"工具 '{name}' 执行失败，请稍后重试"

    def list_tools(self) -> list[str]:
        """返回所有已注册工具名称"""
        return list(self._tools.keys())
