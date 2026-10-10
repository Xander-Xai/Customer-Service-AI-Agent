"""Agent Eval V1 harness：脚本化 LLM + 真实图回放 + 观测采集。

三条不可让步的原则
------------------
1. **跑真图**。观测来自 ``container.graph_app.astream(..., stream_mode="updates")``
   的真实节点执行，而不是对生产逻辑的复述。任何"我按代码写了个模拟器"的实现
   都不算 Agent Eval。
2. **零出网**。唯一的 LLM 是本模块的 :class:`ScriptedAgentLLM` /
   :class:`ScriptedRouterLLM`；它们不接受 ``base_url``、不持有任何 API key，
   因此本 harness 在结构上不可能访问外部 provider。
3. **不改动被测代码**。观测全部走生产已有的通道：LangGraph 的
   ``updates`` 流 + ``core.streaming_context`` 事件 + ``logging`` 记录。
   如果某个行为无法从这些通道观测到，正确答案是"该指标标记为不可测"，
   不是"改生产代码加个钩子"。

观测通道对照
------------
======================  ===================================================
观测量                  通道
======================  ===================================================
节点执行顺序 / 步数     ``astream(stream_mode="updates")`` 分块
路由标签                ``state["query_type"]``
协作模式                ``state["collaboration_mode"]``
工具调用 / 参数          ``stream_callback`` 的 ``type="tool_call"`` 事件
工具执行结果            ``stream_callback`` 的 ``type="tool_result"`` 事件
高风险动作被摘出        ``[HITL] 高风险工具已摘出…`` 日志（cross-check）
降级 / 路由捷径         :data:`evaluation.agent_eval.contract.FALLBACK_MARKERS`
图是否挂在 interrupt 上  返回值里的 ``__interrupt__`` 键
======================  ===================================================

这个 harness 已经抓到一个真实 P0：``collaboration/modes.py::ReActMode`` 曾丢弃
``pending_actions``，导致 HITL 闸门在**真实图**上从未触发，高风险副作用被静默丢弃。
回归锁定在 ``tests/unit/test_hitl_real_graph_gate.py``。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import traceback
from dataclasses import dataclass
from typing import Any

from .cases import AgentCase, ScriptedToolCall
from .contract import (
    FALLBACK_MARKERS,
    HITL_DEFER_MARKER,
    ROUTE_SHORTCUT_CONFIDENCE,
    ROUTE_SHORTCUT_MARKER,
    TOOL_REACHABLE_MODE,
    WAITING_APPROVAL,
)

INTERRUPT_KEY = "__interrupt__"


class _Response:
    """鸭子类型的 LLM 响应（与 ``llm/client.py::CustomResponse`` 同形）。"""

    def __init__(self, content: str, tool_calls: list[dict[str, Any]] | None = None):
        self.content = content
        self.tool_calls = tool_calls


class ScriptedAgentLLM:
    """按数据集脚本回放工具调用计划的确定性 LLM 替身。

    **不**接受 base_url、不持有 API key —— 结构上无法出网。
    """

    def __init__(
        self,
        plan: tuple[ScriptedToolCall, ...],
        final_answer: str,
        fail_on_exhaustion: bool = True,
        fail_first_tool_turn: bool = False,
        always_fail: bool = False,
        time_out: bool = False,
    ):
        self.plan = tuple(plan)
        self.final_answer = final_answer
        self.fail_on_exhaustion = fail_on_exhaustion
        self.fail_first_tool_turn = fail_first_tool_turn
        self.always_fail = always_fail
        self.time_out = time_out
        self._index = 0
        self._turns = 0
        self._issued = 0

    @property
    def plan_exhausted(self) -> bool:
        return self._index >= len(self.plan)

    @property
    def plan_remaining(self) -> int:
        return max(0, len(self.plan) - self._index)

    def _next_tool_call(self) -> list[dict[str, Any]] | None:
        if self._index >= len(self.plan):
            # 计划已用尽有两种截然不同的情况，必须分开：
            #   a) 一条都没发出过 -> 数据集与图对「谁会跑工具」的判断不一致，
            #      这时抛错才能暴露数据集错误；
            #   b) 已经正常发完 -> 工具循环回来说「再给一次回答」，此时**应当**
            #      返回最终回答。真实 Agent 就是这个流程；把它当成错误会让
            #      每一条工具用例都失败。
            if self.fail_on_exhaustion and self._issued == 0:
                raise RuntimeError(
                    "ScriptedAgentLLM received a tool-capable turn for a case that declares no "
                    "scripted tool calls; the dataset and the graph disagree about which agent runs."
                )
            return None
        call = self.plan[self._index]
        self._index += 1
        self._issued += 1
        return [
            {
                "id": f"scripted-{self._index}",
                "name": call.name,
                "arguments": call.arguments_json,
            }
        ]

    async def async_invoke(self, messages, timeout=None, tools=None):
        self._turns += 1
        if self.time_out:
            raise asyncio.TimeoutError("scripted agent LLM timeout (agent-eval)")
        if self.always_fail:
            raise RuntimeError("scripted agent LLM failure (agent-eval)")
        if self.fail_first_tool_turn and self._turns == 1:
            raise RuntimeError("scripted agent LLM failure (agent-eval)")
        if tools:
            tool_calls = self._next_tool_call()
            if tool_calls:
                return _Response("", tool_calls)
        return _Response(self.final_answer, None)

    async def async_invoke_stream(self, messages, timeout=None):
        self._turns += 1
        if self.time_out:
            raise asyncio.TimeoutError("scripted agent LLM timeout (agent-eval)")
        if self.always_fail:
            raise RuntimeError("scripted agent LLM failure (agent-eval)")
        if self.fail_first_tool_turn and self._turns == 1:
            raise RuntimeError("scripted agent LLM failure (agent-eval)")
        yield self.final_answer


class ScriptedRouterLLM:
    """确定性路由器 LLM 替身。"""

    def __init__(self, scripted_route: str | None, confidence: float = 0.95):
        self.scripted_route = scripted_route
        self.confidence = confidence
        self.calls = 0

    def _payload(self) -> str:
        route = self.scripted_route or "general_inquiry"
        return json.dumps({"query_type": route, "confidence": self.confidence}, ensure_ascii=False)

    async def async_invoke(self, messages, timeout=None, tools=None):
        self.calls += 1
        return _Response(self._payload(), None)

    async def async_invoke_stream(self, messages, timeout=None):
        self.calls += 1
        yield self._payload()


class _FailingLLM:
    """按脚本抛错的 LLM 替身。

    必须是**对象**而不是裸函数：路由器调的是 ``self.llm.async_invoke(...)``，
    塞一个普通函数进去会得到 ``'function' object has no attribute
    'async_invoke'`` —— 那是 harness 的接线错误，会被路由器自己的兜底吃掉，
    于是「故障注入」变成了「什么都没发生」，指标假绿。
    """

    def __init__(self, message: str):
        self._message = message

    async def async_invoke(self, messages, timeout=None, tools=None):
        raise RuntimeError(self._message)

    async def async_invoke_stream(self, messages, timeout=None):
        raise RuntimeError(self._message)
        yield ""  # pragma: no cover - async generator 语法需要


class LogSignalProbe(logging.Handler):
    """在 logging 记录上匹配标记，**不改动被测代码**。"""

    def __init__(self, markers: tuple[tuple[str, ...], ...]):
        super().__init__(level=logging.INFO)
        self.markers = tuple(markers)
        self.fired: set[str] = set()
        self.messages: list[str] = []

    @property
    def needles(self) -> tuple[tuple[str, ...], ...]:
        return self.markers

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # pragma: no cover - 格式化失败不得影响被测代码
            return
        self.messages.append(text)
        for needle_set in self.markers:
            if all(needle in text for needle in needle_set):
                self.fired.add(needle_set[0])


def _loggers_for_prefix(prefix: str) -> list[logging.Logger]:
    """找出所有名字以 ``prefix.`` 开头的 logger（含具体 Agent logger）。"""
    root = logging.getLogger()
    return [
        lg
        for lg in [root, *list(root.manager.loggerDict.values())]
        if isinstance(lg, logging.Logger) and lg.name.split(".")[0] == prefix
    ]


def attach_probes(
    probes: tuple[LogSignalProbe, ...],
) -> tuple[tuple[logging.Logger, LogSignalProbe], ...]:
    attached = []
    for probe in probes:
        # 覆盖所有可能记录降级的 logger 树：Agent 兜底、路由器降级、
        # LLM 客户端降级。只挂 agents/ 会漏掉后两类 —— 而"降级率"这个指标
        # 恰恰最容易在"只查一处日志"的情况下显得比实际更干净。
        for prefix in ("agents", "router", "llm", "core", "collaboration"):
            for logger in _loggers_for_prefix(prefix):
                logger.addHandler(probe)
                attached.append((logger, probe))
    return tuple(attached)


def detach_probes(attached) -> None:
    for logger, probe in attached:
        logger.removeHandler(probe)


@dataclass(frozen=True)
class ToolCallObservation:
    """一次被发起的工具调用及其治理结果。"""

    name: str
    arguments: dict[str, Any]
    policy_risk: str
    policy_requires_approval: bool
    schema_errors: tuple[str, ...]
    executed: bool
    deferred: bool
    unknown_tool: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "policy_risk": self.policy_risk,
            "policy_requires_approval": self.policy_requires_approval,
            "schema_errors": list(self.schema_errors),
            "executed": self.executed,
            "deferred": self.deferred,
            "unknown_tool": self.unknown_tool,
        }


@dataclass(frozen=True)
class CaseObservation:
    """一条 case 的完整观测。所有指标只从这一个对象派生。"""

    case_id: str
    nodes_executed: tuple[str, ...]
    step_count: int
    observed_route: str | None
    route_source: str
    rule_route: str | None
    rule_confidence: float
    router_llm_calls: int
    observed_mode: str | None
    resolution_status: str | None
    response: str
    terminal_state: str
    tool_calls: tuple[ToolCallObservation, ...]
    fallback_markers: tuple[str, ...]
    route_shortcut_used: bool
    hitl_defer_logged: bool
    pending_actions: tuple[dict[str, Any], ...]
    interrupt_payloads: tuple[dict[str, Any], ...]
    side_effect_counters: dict[str, int]
    scripted_plan_remaining: int
    scripted_tool_rounds: int
    elapsed_ms: float
    pinned_mode: str | None = None
    error: str | None = None
    error_traceback: str | None = None

    @property
    def requested_tool_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.tool_calls)

    @property
    def executed_tool_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.tool_calls if c.executed)

    @property
    def deferred_tool_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.tool_calls if c.deferred)

    @property
    def side_effect_total(self) -> int:
        return sum(self.side_effect_counters.values())

    @property
    def tool_loop_reachable(self) -> bool:
        """该 case 的工具循环是否真的被走到（工具指标的分母前置条件）。"""
        return self.observed_mode == TOOL_REACHABLE_MODE

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "nodes_executed": list(self.nodes_executed),
            "step_count": self.step_count,
            "observed_route": self.observed_route,
            "route_source": self.route_source,
            "rule_route": self.rule_route,
            "rule_confidence": self.rule_confidence,
            "router_llm_calls": self.router_llm_calls,
            "observed_mode": self.observed_mode,
            "resolution_status": self.resolution_status,
            "terminal_state": self.terminal_state,
            "response": self.response,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "executed_tool_names": list(self.executed_tool_names),
            "deferred_tool_names": list(self.deferred_tool_names),
            "fallback_markers": list(self.fallback_markers),
            "route_shortcut_used": self.route_shortcut_used,
            "hitl_defer_logged": self.hitl_defer_logged,
            "pending_actions": list(self.pending_actions),
            "interrupt_payloads": list(self.interrupt_payloads),
            "side_effect_counters": dict(self.side_effect_counters),
            "side_effect_total": self.side_effect_total,
            "scripted_plan_remaining": self.scripted_plan_remaining,
            "scripted_tool_rounds": self.scripted_tool_rounds,
            "elapsed_ms": self.elapsed_ms,
            "pinned_mode": self.pinned_mode,
            "error": self.error,
            "error_traceback": self.error_traceback,
        }


def _ascii_slug(value: str, *, limit: int = 48) -> str:
    """把任意 case_id 转成 ``SessionManager`` 认可的 ASCII session_id。

    ``core/session/session_manager.py`` 只接受 ``[A-Za-z0-9_-]{1,128}``；非法
    id 会被静默换成一个新的 UUID，随后的上下文查询必然落空。数据集里有中文
    case_id（按业务分类命名），直接当 session_id 用会让每一条都走进那条
    "会话不存在" 的路径 —— 那是 harness 的 id 造错了，不是被测系统的行为。
    保留可读前缀 + 稳定哈希，既可读又不丢唯一性。
    """
    safe = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "_-")) else "-" for ch in value)
    safe = safe.strip("-") or "case"
    if len(safe) > limit:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:limit]}-{digest}"
    return safe


def _final_answer_for(case: AgentCase) -> str:
    return (
        f"[agent-eval] 已完成对用例 {case.case_id} 的处理："
        "订单与产品信息已核对完毕，如需进一步协助请随时告知。"
    )


def _route_source(observed: dict[str, Any], router_calls: int) -> str:
    if observed.get("cached"):
        return "cached"
    raw = str(observed.get("route_source") or "")
    if raw == "rule_shortcut":
        return "rule_shortcut"
    if raw == "[circuit_breaker_open]":
        return "circuit_breaker_open"
    if raw == "[error_fallback]":
        return "error_fallback"
    if observed.get("query_type") in (None, "", "unavailable"):
        return "unavailable"
    return "llm_arbitrated" if router_calls else "rule_shortcut"


def _terminal_state(observed: dict[str, Any], pending_actions, interrupts) -> str:
    if observed.get("error"):
        return "FAILED"
    if interrupts or observed.get("pending_actions"):
        return WAITING_APPROVAL
    return "SUCCEEDED"


def _detect_fallbacks(log_fired: set[str], observed: dict[str, Any]) -> tuple[str, ...]:
    """判定本次执行是否发生了降级 —— **两条通道都要查**。

    - **日志通道**：路由降级（``[circuit_breaker_open]`` / ``[error_fallback]``）、
      Agent 未注册等会打日志的路径。
    - **响应文本通道**：大量降级路径**根本不打日志**，只是把
      ``fallback_response`` 当作**返回值**交出去
      （见 ``agents/*_agent.py`` 的 ``fallback_response=`` 参数）。

    只查日志会把第二类判成"没有降级"，而用户实际拿到的是一句
    "抱歉，处理问题时遇到错误" —— 这是最不该被漏掉的一种降级。
    反过来只查文本又会漏掉路由降级（响应看起来完全正常）。

    两条并集即为结论；去重后返回，保证 ``fallback_rate`` 的
    "一个 case 只计一次" 语义成立。
    """
    fired = set(log_fired)
    response_text = str(observed.get("response") or "")
    if response_text:
        for name, _meaning in FALLBACK_MARKERS:
            if name in response_text:
                fired.add(name)
    return tuple(sorted(fired))


class AgentEvalHarness:
    """在真实容器 / 真实编译图上回放 case。"""

    def __init__(
        self,
        *,
        hitl_enabled: bool = True,
        register_staging_tools: bool = True,
        thread_prefix: str = "agent-eval",
    ):
        self.hitl_enabled = hitl_enabled
        self.register_staging_tools = register_staging_tools
        self.thread_prefix = thread_prefix
        self.container: Any = None
        self.agent_llm: Any = None
        self.router_llm: Any = None
        self._probes: tuple[LogSignalProbe, ...] = ()
        self._attached: tuple[tuple[logging.Logger, LogSignalProbe], ...] = ()
        self.pinned_mode: str | None = None
        self._restore_mode: bool = False
        self._original_select: Any = None

    # ── lifecycle ───────────────────────────────────────────────────────
    async def start(self) -> None:
        # HITL 的开关在**调用时**从 ``core.config`` 读（``core/hitl/gate.py``），
        # 因此必须在图开始跑之前把进程内的值打开。忘了这一步，harness 会
        # 「看起来在跑治理指标」而实际一条都没拦 —— 那正是本 harness 要度量的
        # 东西，绝不能因为一个开关而悄悄全绿。
        import core.config as _config
        from core.container import ServiceContainer

        _config.HITL_ENABLED = self.hitl_enabled
        if self.hitl_enabled:
            _config.HITL_HIGH_RISK_TOOLS = "staging_refund,staging_order_change"
            _config.HITL_HIGH_AMOUNT_THRESHOLD = 0.0

        self.container = ServiceContainer()
        await self.container.initialize()

        if self.register_staging_tools:
            from tools.hitl_staging_tools import register_hitl_staging_tools

            register_hitl_staging_tools(self.container.tool_registry)

        self._probes = (
            LogSignalProbe(tuple((name,) for name, _ in FALLBACK_MARKERS)),
            LogSignalProbe((ROUTE_SHORTCUT_MARKER,)),
            LogSignalProbe((HITL_DEFER_MARKER[:1],)),
        )
        self._attached = attach_probes(self._probes)

    async def close(self) -> None:
        detach_probes(self._attached)
        self._attached = ()
        if self.container is not None:
            await self.container.close()
            self.container = None

    async def __aenter__(self) -> AgentEvalHarness:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, exc_info) -> None:
        await self.close()

    # ── injection ───────────────────────────────────────────────────────
    def _inject_llms(self, case: AgentCase) -> None:
        """把脚本化 LLM 注入**每一个** Agent 与路由器。

        Agent 在构造期就捕获了 ``container.llm``，因此必须在 ``initialize()``
        之后重新注入。忘了这一步会让 harness「看起来在跑」但实际走模板兜底 ——
        这是本 harness 最早的一个静默失败模式，故在 ``start()`` 之后立刻注入，
        并由 ``run_case`` 断言 events 与观测的一致性。
        """
        self.agent_llm = ScriptedAgentLLM(
            case.scripted_tool_calls,
            final_answer=_final_answer_for(case),
            # 什么时候「计划用尽」算**错误**？
            #
            # 只有当 case 明确断言"不许调任何工具"（声明了 forbidden_tools）时，
            # 图还来要工具才说明治理边界失守。其余情况（case 只是没脚本化工具，
            # 而编排器恰好选了 react）属于**模式选择**的结果 —— 模式选择不是
            # 本套件要度量的东西，为此让 LLM 抛错只会把一条正常 case 记成降级。
            # 之前用 ``not case.expect_fallback`` 做触发条件，正是因此把
            # route_多意图组合_082 这类 case 误判成了降级。
            fail_on_exhaustion=bool(case.forbidden_tools),
            fail_first_tool_turn=case.scripted_failure == "agent_llm_error_first_turn",
            always_fail=case.scripted_failure == "agent_llm_error",
            time_out=case.scripted_failure == "agent_llm_timeout",
        )
        self.router_llm = ScriptedRouterLLM(case.scripted_route)

        for agent in (getattr(self.container, "agents_dict", None) or {}).values():
            agent.llm = self.agent_llm
        response_agent = getattr(self.container, "response_agent", None)
        if response_agent is not None:
            response_agent.llm = self.agent_llm
        # 把「有脚本化工具调用」的 case 钉在工具可达模式（react）。
        #
        # 理由与边界：这里度量的是**编排层是否执行了脚本计划**，不是「编排器是否会
        # 选 react」。自然选择下 `react` 需要 complexity >= 阈值且 multi_agent
        # hints >= 2，绝大多数工具类查询会落进 sequential —— 那会让工具指标恒为
        # 不可达（分母 0 -> NOT_MEASURED），而不是产出有意义的数字。
        #
        # 钉住这件事必须**可见**：本字段进 observation、artifact 里能看到哪些
        # case 被钉过，且 mode 选择本身仍以 `observed_mode` 原样记录。
        orchestrator = getattr(self.container, "orchestrator", None)
        if orchestrator is not None:
            # 每个 case 之前先恢复原实现：钉住是**逐 case**的，不能泄漏到下一条。
            if self._original_select is None:
                self._original_select = orchestrator.select_mode_name
            else:
                orchestrator.select_mode_name = self._original_select
            self.pinned_mode = None
            if case.scripted_tool_calls:
                self.pinned_mode = TOOL_REACHABLE_MODE
                orchestrator.select_mode_name = lambda *a, **k: TOOL_REACHABLE_MODE

        router = getattr(self.container, "router", None)
        if router is not None:
            if case.scripted_failure == "router_llm_error":
                router.llm = _FailingLLM("scripted router LLM failure (agent-eval)")
            else:
                router.llm = self.router_llm

    # ── per-case run ────────────────────────────────────────────────────
    async def run_case(self, case: AgentCase) -> CaseObservation:
        if self.container is None:
            raise RuntimeError("harness not started; use `async with AgentEvalHarness() as h:`")

        from core.streaming_context import reset_stream_callback, set_stream_callback
        from runtime.context import reset_run_context, set_run_context
        from tools.hitl_staging_tools import reset_staging_ledger, staging_ledger

        self._inject_llms(case)
        # **每条 case 前清空响应缓存。**
        #
        # Layer-0 缓存命中会在 ``check_cache`` 直接短路到 ``final_response``：
        # 路由器、Agent、工具循环**全部不执行**。跨 case 复用容器时，前一条
        # case 的回答会被后一条（哪怕语义相近）命中，于是 HIGH 风险 case
        # 根本走不到工具循环与 HITL 闸门 —— 表现为 ``task_completion_rate``
        # 莫名下跌，且与被测系统无关。
        #
        # 这不是"为了让分数好看而关掉检查"：缓存命中本身是**另一个**被测能力
        # （它有自己的 benchmark 与契约），混进来只会污染本套件的语义。
        cache = getattr(self.container, "cache", None)
        if cache is not None:
            with contextlib.suppress(Exception):
                cache.clear()
        reset_staging_ledger()
        for probe in self._probes:
            probe.fired.clear()

        events: list[dict[str, Any]] = []

        async def _collector(event):
            if isinstance(event, dict):
                events.append(event)
            return None

        slug = _ascii_slug(case.case_id)
        thread_id = f"{self.thread_prefix}-thread-{slug}"
        run_id = f"{self.thread_prefix}-run-{slug}"
        observed: dict[str, Any] = {}
        nodes: list[str] = []
        interrupts: list[Any] = []
        error: str | None = None
        error_traceback: str | None = None

        def _on_interrupt(value):
            interrupts.append(value)

        from tools.tool_registry import (
            ToolRegistry,  # noqa: F401  (registry for _collect_tool_calls)
        )

        token_cb = set_stream_callback(_collector)
        tokens = set_run_context(run_id, thread_id)
        started = time.perf_counter()
        try:
            stream = self.container.graph_app.astream(
                {
                    "session_id": thread_id,
                    "customer_query": case.input,
                    "user_id": f"{self.thread_prefix}-user:{case.case_id}",
                    "trace_id": f"{self.thread_prefix}-trace",
                },
                config={"configurable": {"thread_id": thread_id}},
                stream_mode="updates",
            )
            async for update in stream:
                for node, value in update.items():
                    if node == INTERRUPT_KEY:
                        _on_interrupt(value)
                        continue
                    nodes.append(node)
                    if isinstance(value, dict):
                        observed.update(value)
        except Exception as exc:  # noqa: BLE001 - 故障注入用例预期会抛
            # 记录 traceback 而不是只记一行消息：一个没有栈的异常无法排障，
            # 而 harness 自身出错与被测代码出错在这里长得一模一样。
            error = f"{type(exc).__name__}: {exc}"
            error_traceback = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:]
            )
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            reset_stream_callback(token_cb)
            reset_run_context(tokens)

        pending_actions = tuple(observed.get("pending_actions") or ())
        interrupt_payloads = tuple(getattr(item, "value", None) or {} for item in interrupts)
        interrupt_payloads = tuple(p for p in interrupt_payloads if isinstance(p, dict))

        registry = getattr(self.container, "tool_registry", None)
        tool_calls = self._collect_tool_calls(events, registry, pending_actions)

        deferred_by_log = any(HITL_DEFER_MARKER[0] in p.fired for p in self._probes) or any(
            HITL_DEFER_MARKER[0] in m for m in self._probes[-1].messages
        )
        fallback_markers = _detect_fallbacks(self._probes[0].fired, observed)
        route_shortcut = any(ROUTE_SHORTCUT_MARKER[0] in m for m in self._probes[1].messages)

        observation = CaseObservation(
            case_id=case.case_id,
            nodes_executed=tuple(nodes),
            step_count=len(nodes),
            observed_route=observed.get("query_type"),
            route_source=_route_source(observed, getattr(self.router_llm, "calls", 0)),
            rule_route=None,
            rule_confidence=float(observed.get("rule_confidence") or 0.0),
            router_llm_calls=int(getattr(self.router_llm, "calls", 0)),
            observed_mode=observed.get("collaboration_mode"),
            resolution_status=observed.get("resolution_status"),
            response=str(observed.get("response") or ""),
            terminal_state=_terminal_state(observed, pending_actions, interrupts),
            tool_calls=tool_calls,
            fallback_markers=fallback_markers,
            route_shortcut_used=route_shortcut,
            hitl_defer_logged=deferred_by_log,
            pending_actions=pending_actions,
            interrupt_payloads=interrupt_payloads,
            side_effect_counters=(
                {key: int(entry.get("calls", 0)) for key, entry in staging_ledger().items()}
                if self.register_staging_tools
                else {}
            ),
            scripted_plan_remaining=int(getattr(self.agent_llm, "plan_remaining", 0)),
            scripted_tool_rounds=len([e for e in events if e.get("type") == "tool_call"]),
            elapsed_ms=round(elapsed_ms, 3),
            pinned_mode=self.pinned_mode,
            error=error,
            error_traceback=error_traceback,
        )
        return observation

    # ── tool call analysis ──────────────────────────────────────────────
    def _collect_tool_calls(
        self,
        events: list[dict[str, Any]],
        registry: Any,
        pending_actions: tuple[dict[str, Any], ...],
    ) -> tuple[ToolCallObservation, ...]:
        """把 tool_call / tool_result 事件 + pending_actions 归并成逐次调用观测。

        「执行了没有」不是看有没有 tool_result 事件，而是看**治理判定**：
        被 HITL 摘下的调用不会有 tool_result（它根本没跑），未摘出的调用才有。
        用「摘出的集合」与「已发起集合」求差，比等事件更可靠 —— 事件流本身
        也可能因为降级路径而缺失。
        """
        from core.hitl.risk import classify_risk, requires_approval

        from .schema_check import SchemaError, validate_arguments

        deferred_names = {str(p.get("tool")) for p in pending_actions}
        results_by_name: dict[str, str] = {}
        for event in events:
            if event.get("type") == "tool_result":
                results_by_name[str(event.get("name"))] = str(event.get("summary"))

        observed: list[ToolCallObservation] = []
        for event in events:
            if event.get("type") != "tool_call":
                continue
            name = str(event.get("name"))
            args = event.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            declared = registry is not None and name in (registry.list_tools() or [])
            explicit = registry.risk_level_for(name) if declared else None
            side_effect = bool(registry.is_side_effect(name)) if declared else False
            policy_risk = classify_risk(
                name, explicit=explicit, arguments=args, side_effect=side_effect
            ).value
            policy_requires_approval = requires_approval(policy_risk)
            schema_errors: tuple[str, ...] = ()
            if declared:
                try:
                    tool_def = registry._tools[name]  # noqa: SLF001 - 只读取证
                    schema_errors = tuple(validate_arguments(args, tool_def.parameters))
                except (SchemaError, KeyError, AttributeError) as exc:
                    schema_errors = (f"schema_check_failed: {exc}",)
            deferred = name in deferred_names
            observed.append(
                ToolCallObservation(
                    name=name,
                    arguments=args,
                    policy_risk=policy_risk,
                    policy_requires_approval=policy_requires_approval,
                    schema_errors=schema_errors,
                    executed=not deferred and name in results_by_name,
                    deferred=deferred,
                    unknown_tool=not declared,
                )
            )
        return tuple(observed)


__all__ = [
    "AgentEvalHarness",
    "CaseObservation",
    "INTERRUPT_KEY",
    "LogSignalProbe",
    "ROUTE_SHORTCUT_CONFIDENCE",
    "ScriptedAgentLLM",
    "ScriptedRouterLLM",
    "ToolCallObservation",
]
