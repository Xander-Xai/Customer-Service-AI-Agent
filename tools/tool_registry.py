"""
工具注册与执行框架（v3.5）
支持 OpenAI Function Calling 格式的工具定义、注册和执行。
"""

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.logger import get_logger
from core.tool_result_cache import ToolCachePolicy

logger = get_logger("tools.registry")


def _tool_timeout_seconds() -> float:
    """读当前生效的工具超时（每次调用读取，便于测试与运行时调整）。

    读不到配置时返回 0 = 不加超时包装，**保持历史行为**：治理配置缺失不应该
    悄悄改变既有工具的语义。配置校验在 ``core.config.validate_tool_settings``。
    """
    try:
        from core.config import TOOL_EXECUTION_TIMEOUT_SECONDS

        return float(TOOL_EXECUTION_TIMEOUT_SECONDS)
    except Exception:
        return 0.0


def _observe_tool_execution(outcome: str, tool_name: str, elapsed: float) -> None:
    """记录工具执行结果与耗时（无 label 敏感信息；失败绝不冒泡）。

    ``outcome`` 是**有界**词表（ok / timeout / error），``tool_name`` 来自注册表，
    因此不会引入无界时序。指标不可用时静默降级 —— 观测失败绝不能变成业务失败。
    """
    try:
        from core.monitoring import (
            tool_execution_duration_seconds,
            tool_execution_timeout_total,
            tool_execution_total,
        )

        tool_execution_total.labels(outcome=outcome).inc()
        tool_execution_duration_seconds.labels(tool_name=tool_name).observe(elapsed)
        if outcome == "timeout":
            tool_execution_timeout_total.inc()
    except Exception:  # noqa: BLE001 - 指标失败不得影响工具执行
        return


#: 工具来源。native = 进程内 Function Calling 工具；mcp = 外部 MCP server 工具
#: （经 ``tools/mcp_adapter.py`` 叠加进同一个注册表）。
#:
#: ``source`` 纯粹是**可观测性**维度：它不改变执行语义、不影响风险等级、不参与
#: 幂等或审批判定。用途是让「哪些工具来自不可信的外部进程」在注册表层面可见
#: （审计、指标、debug），而不是散落在调用日志里。
SOURCE_NATIVE = "native"
SOURCE_MCP = "mcp"


class ToolExecutionTimeout(Exception):
    """原生工具的单次执行超时。

    刻意**不**继承 ``runtime.errors`` 的异常：工具层不能依赖 runtime 包（``tools``
    与 ``runtime`` 是两个独立边界），否则会形成单向依赖并让工具层无法单独使用。

    但它**必须**能被上层的错误分类识别为「可重试 / 结果未知」，因此携带一个
    ``timeout`` 标记；``runtime`` 侧的分类逻辑按属性识别（见
    ``tests/unit/test_tool_execution_reliability.py``）。

    语义是「执行结果未知」而不是「执行失败」：外部系统可能已经写成功了一部分。
    把它当失败重试可能造成重复写，把它当成功会丢数据 —— 所以唯一安全的做法是
    上抛，交给幂等 ledger + run 级 retry/DLQ + 人工重放处理。
    """

    #: 上层按属性识别（而不是 isinstance，避免跨包继承）
    is_tool_timeout = True

    def __init__(self, tool_name: str, timeout_seconds: float, message: str):
        super().__init__(message)
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds


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
    source: str = SOURCE_NATIVE
    """工具来源标签（native / mcp），仅用于可观测性，详见 ``SOURCE_NATIVE``。"""


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
        source: str = SOURCE_NATIVE,
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
            source=source,
        )
        logger.debug(f"工具已注册: {name}")

    def unregister(self, name: str) -> bool:
        """撤销注册，返回是否真的移除过一个工具。

        存在的唯一理由是**注册的事务性**：MCP 工具是运行时叠加进同一个注册表的
        （见 ``tools.mcp_adapter.register_mcp_tools``），一次多 server 初始化可能
        前一个 server 已注册成功、后一个失败。fail-closed 时必须能把「本次 attempt
        新增的那些」撤销掉，否则注册表会留下指向已关闭 adapter 的工具 ——
        LLM 仍会看到并调用它们，只会在调用瞬间炸。

        未注册的名字是 **no-op**（返回 ``False``）而不是抛错：回滚路径必须能对
        「可能已经删过了」的名字安全重试，且撤销失败不应掩盖真正的初始化错误。
        """
        if name in self._tools:
            del self._tools[name]
            logger.debug(f"工具已注销: {name}")
            return True
        return False

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
        self,
        name: str,
        arguments: dict[str, Any],
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

        # Fail-closed governance boundary (Issue #123). This is the single choke
        # point every realtime transport (REST / SSE / WS / multimodal) and the
        # async worker funnel through for tool execution. A declared side-effect
        # tool with no durable Run context has neither human approval nor the
        # side-effect idempotency ledger, so it must be refused *before* the
        # handler runs — not merely discouraged via prompt text.
        refusal = self._ungoverned_side_effect_refusal(tool, name, arguments)
        if refusal is not None:
            logger.warning(
                "拒绝无治理边界的写操作工具调用 tool=%s side_effect=%s",
                name,
                tool.side_effect,
            )
            return refusal

        # Lightweight tracing: one span per tool execution. Records the tool NAME,
        # whether it is a declared side effect, and its risk level — never
        # ``arguments``. This system's tool arguments include order numbers, refund
        # amounts and customer identifiers, i.e. exactly the content that must not
        # reach a trace backend (see core/telemetry).
        #
        # Two-level resolution: full implementation first, then a no-op fallback
        # that imports nothing. A diagnostic-only dependency failing to import must
        # not be able to fail a business tool call — especially a side-effecting
        # one. See core/tracing.py::safe_span.
        try:
            from core.telemetry import span as _telemetry_span
        except Exception:  # noqa: BLE001 - diagnostics must never break the runtime
            from core.tracing import safe_span as _telemetry_span

        with _telemetry_span(
            "csai.tool.execute",
            attributes={
                "csai.tool_name": name,
                "csai.tool_side_effect": bool(tool.side_effect),
                "csai.risk_level": tool.risk_level,
            },
        ):
            return await self._execute_raw_inner(
                tool, name, arguments, stream_callback, tool_call_id
            )

    def _ungoverned_side_effect_refusal(
        self, tool: ToolDefinition, name: str, arguments: dict[str, Any]
    ) -> str | None:
        """写操作无治理边界时返回错误文案；否则返回 None 放行。

        fail-closed：治理模块不可导入、run 上下文不可判定、风险不可分级，任一
        「无法确认安全」的情况都对**已声明副作用**的工具拒绝执行；只读工具不受影响。
        """
        if not tool.side_effect:
            return None
        try:
            from core.hitl.gate import (
                is_ungoverned_side_effect,
                ungoverned_side_effect_message,
            )
        except Exception:
            # Governance unavailable -> cannot prove safety for a write op.
            return f"错误：写操作工具 '{name}' 无法确认安全治理边界，已拒绝执行（治理模块不可用）。"
        try:
            if is_ungoverned_side_effect(name, arguments, self):
                return ungoverned_side_effect_message(name)
        except Exception:
            return f"错误：写操作工具 '{name}' 无法确认安全治理边界，已拒绝执行（治理判定失败）。"
        return None

    async def _execute_raw_inner(
        self,
        tool: ToolDefinition,
        name: str,
        arguments: dict[str, Any],
        stream_callback: Callable | None,
        tool_call_id: str | None,
    ) -> Any:
        """``execute_raw`` proper, with the span wrapper peeled off."""
        idempotent_op, refusal = self._idempotent_operation(
            tool, arguments, tool_call_id, stream_callback=stream_callback
        )
        if refusal is not None:
            # 异步 Run 中的写操作无法构造可靠的持久化幂等保护 -> FAIL CLOSED。
            # 绝不退化成裸 handler 执行：at-least-once 重投会重复副作用。
            logger.warning("异步 Run 副作用前置条件缺失，拒绝执行 tool=%s", name)
            return refusal
        if idempotent_op is not None:
            # 副作用工具的失败必须冒泡：吞掉异常会把「写操作失败」伪装成成功 run，
            # 让上层 retry/DLQ 完全失效。
            return await idempotent_op()

        try:
            # 只读工具走同一条带超时的调用路径（`_call_handler`）：超时是工具执行的
            # **通用**边界，不该只对某一条分支生效。副作用工具的闭包 `_run` 内部
            # 同样调用 `_call_handler`，因此两条分支的超时语义完全一致。
            result = await self._call_handler(tool, arguments, stream_callback)
            return result if result is not None else "查询完成，无结果"
        except Exception as e:
            logger.error(f"工具执行失败 [{name}]: {e}", exc_info=True)
            _observe_tool_execution("error", name, 0.0)
            return f"工具 '{name}' 执行失败，请稍后重试"

    def _idempotent_operation(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str | None,
        *,
        stream_callback: Callable | None = None,
    ) -> tuple[Callable[[], Any] | None, str | None]:
        """为副作用工具构造幂等执行闭包；返回 ``(operation, refusal)``。

        判定（Issue #130，异步 Run 前置条件 fail-closed）：

          - **只读工具**：``(None, None)``，直接走 handler。
          - **已声明副作用 + 无 Run 上下文**（实时快路径）：``(None, None)``。
            该路径的治理由 ``ToolRegistry.execute_raw`` 的统一边界负责
            （Issue #123），本函数不在此扩张职责。
          - **已声明副作用 + Run 上下文 + 幂等前置条件齐备**：``(_run, None)``，
            经 ``runtime.side_effects`` ledger 执行，重投递去重。
          - **已声明副作用 + Run 上下文 + 前置条件缺失**：``(None, refusal)``。
            缺少 ``tool_call_id``、Run 上下文模块不可用、幂等 ledger 模块不可用、
            操作键无法构造——任一情况都**拒绝执行**。

        为什么缺失前置条件必须 fail-closed
        ---------------------------------
        异步 Run 是 at-least-once：worker 崩溃/重投会重放同一逻辑步骤。写操作一旦
        在没有操作键或没有 ledger 的情况下裸执行，就无法证明「做了一次不会被重做」。
        历史上的实现在这些情况下 ``return None`` 静默退化成非幂等直调，等于把
        「保护不可用」伪装成「无需保护」。宁可让调用方看到明确拒绝/失败，也不能
        产生未被记录、可能重复的副作用。
        """
        if not tool.side_effect:
            return None, None
        try:
            from runtime.context import get_current_run_id, get_current_thread_id
        except Exception:
            # Run 上下文模块不可用：无法证明处于受治理的 Run 中，保守拒绝写操作。
            return None, self._missing_precondition_refusal(tool.name, "Run 上下文模块不可用")
        run_id = get_current_run_id()
        if not run_id:
            # 实时快路径：由统一执行边界的治理判定处理（Issue #123）。
            return None, None
        if not tool_call_id:
            return None, self._missing_precondition_refusal(tool.name, "缺少 tool_call_id")
        try:
            from runtime.side_effects import (
                build_tool_idempotency_key,
                execute_idempotent_operation,
            )
        except Exception:
            logger.error(
                "side-effect ledger 不可用，拒绝在 Run 上下文中裸执行副作用: %s", tool.name
            )
            return None, self._missing_precondition_refusal(tool.name, "幂等 ledger 不可用")
        try:
            operation_key = build_tool_idempotency_key(run_id, tool_call_id)
        except Exception:
            return None, self._missing_precondition_refusal(tool.name, "幂等操作键构造失败")
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
                # 副作用工具超时时**必须冒泡**：ledger 只有在收到异常时才会把这次
                # 执行标记为 failed。若在这里把超时转成字符串返回，ledger 会把
                # 一个「结果未知」的写操作记成 SUCCEEDED 并缓存，重投递时直接返回
                # 那条字符串 —— 写操作从此再也不会被执行，而 run 表面成功。
                operation=lambda: self._call_handler(
                    tool, arguments, stream_callback, raise_on_timeout=True
                ),
            )

        return _run, None

    @staticmethod
    def _missing_precondition_refusal(tool_name: str, reason: str) -> str:
        """异步 Run 写操作缺少可靠幂等保护时的明确拒绝语义。"""
        return (
            f"错误：写操作工具 '{tool_name}' 在异步 Run 中缺少可靠的幂等保护"
            f"（{reason}），已拒绝执行以保持 at-least-once 下的副作用安全。"
        )

    @staticmethod
    async def _call_handler(
        tool: ToolDefinition,
        arguments: dict[str, Any],
        stream_callback: Callable | None,
        *,
        raise_on_timeout: bool = False,
    ) -> Any:
        """调用 handler，套一层 per-call 超时。

        为什么必须有超时
        --------------
        此前**只有** MCP 工具有 per-call timeout（``tools/mcp_adapter.py`` 用
        ``asyncio.wait_for``），原生工具（ERP / 内部查询）完全裸跑。一个卡住的
        ERP 连接会一路占着 Agent 的工具循环、run 的 ownership lease 与 Redis
        thread lock，直到 Celery 的 ``AGENT_RUN_TASK_TIME_LIMIT`` 把整个 run 掐掉
        —— 单个慢工具被升级成「整条 run 失败 + 重投 + 租约重分配」。

        ``raise_on_timeout`` —— 为什么两种路径的**超时语义必须不同**
        --------------------------------------------------------
        - **只读工具**（``raise_on_timeout=False``，默认）：超时降级为一条可解释的
          错误字符串。工具失败对 Agent 而言是「这条策略走不通，去换一条或如实
          告知用户」，不是「整个请求崩掉」。
        - **副作用工具**（``raise_on_timeout=True``）：超时必须**冒泡**。

          这里曾经写反过一次，后果是严重的：超时被转成字符串返回后，
          ``runtime.side_effects.execute_idempotent_operation`` 收到的是一个
          **正常返回值**，于是把这次「根本没执行完」的写操作标记为
          ``SUCCEEDED`` 并缓存结果。重投递时 ledger 直接返回那条错误字符串，
          副作用**再也不会被重试**，而 run 看起来是成功的 ——
          「写操作丢失」被伪装成「工具返回了错误提示」。

          也就是说：超时对副作用工具而言是**未知的执行结果**（可能写了一半），
          正确做法是让异常上抛 —— ledger 标记 failed 并把不确定性留给 run 级
          的 retry / DLQ 与人工重放去处理，而不是由工具层擅自判定成功。

        超时值 <= 0 时**完全不加包装**（保留历史行为），这样单元测试里的同步/极慢
        handler 不会被静默截断。
        """
        timeout = _tool_timeout_seconds()
        if timeout <= 0:
            return await ToolRegistry._invoke_handler(tool, arguments, stream_callback)
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                ToolRegistry._invoke_handler(tool, arguments, stream_callback),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            logger.error("工具执行超时 [%s]: 超过 %s", tool.name, f"{timeout:g}s")
            _observe_tool_execution("timeout", tool.name, time.perf_counter() - started)
            message = f"工具 '{tool.name}' 执行超时（>{timeout:g}s），已中止本次调用"
            if raise_on_timeout:
                raise ToolExecutionTimeout(tool.name, timeout, message) from exc
            return f"错误：{message}。请稍后重试或改用其它查询方式。"
        _observe_tool_execution("ok", tool.name, time.perf_counter() - started)
        return result

    @staticmethod
    async def _invoke_handler(
        tool: ToolDefinition, arguments: dict[str, Any], stream_callback: Callable | None
    ) -> Any:
        """真正的 handler 调用（注入 stream_callback，仅当 handler 接受该参数时）。"""
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

    def source_for(self, name: str) -> str | None:
        """该工具的来源（native / mcp）；不存在返回 None。"""
        tool = self._tools.get(name)
        return tool.source if tool else None

    def tools_by_source(self, source: str) -> list[str]:
        """按来源返回工具名（保持注册顺序）。未知来源返回空列表。"""
        return [tool.name for tool in self._tools.values() if tool.source == source]
