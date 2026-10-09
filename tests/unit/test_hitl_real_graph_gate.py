"""HITL 审批闸门在**真实编译图**上的端到端回归（本轮修复的 P0）。

Bug 本体
--------
``BaseAgent._process_with_tools`` 把 HIGH 风险副作用摘出到
``state["pending_actions"]``（**不执行**），``core/graph_builder.py`` 也会把它
合并进图状态并交给 ``human_approval_gate`` 节点。但
``collaboration/modes.py::ReActMode.execute`` 只返回
``{response, mode, agents_used, elapsed}`` —— Agent 写的是它自己那份
``dict(state)`` 副本，因此 ``pending_actions`` **从未离开 Agent**。

结果（在修复前于真实图上实测）：

    [HITL] 高风险工具已摘出待人工审批 tool=staging_refund
    HITL interrupt?  : False
    pending_actions? : None
    SIDE EFFECTS RUN : 0

被摘出的退款**既没执行、也没审批**，而 run 照常走到 ``final_response`` 并报告
成功。这比「不拦」更危险：不拦会执行，静默丢弃会让用户的钱无声消失，同时
SLA / 监控把这次会话记成 resolved。

为什么仓库里已有的 HITL 测试没抓到
----------------------------------
``tests/integration/runtime/test_hitl_langgraph_interrupt.py`` 用的是**自己搭的
最小图**（只含 gate 节点），直接驱动 ``run_approval_gate``。它验证的是
LangGraph 的 interrupt/resume 语义，**完全覆盖不到**「协作模式 → 图节点」这条
接缝。真实图上的这条路径此前**没有任何测试**。

本文件补的就是这个缺口：驱动 ``container.graph_app``（生产编译产物），
断言摘出 → 透传 → interrupt 的完整链路，并断言副作用**一次都没发生**。

同时断言「不得靠默认放行来让测试通过」：不设 run 上下文时审批**不可达**
（闸门明确不拦），但 ``ToolRegistry`` 会**拒绝**无治理边界的写操作 ——
两条机制各司其职，不得互相冒充。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.unit


class _ScriptedLLM:
    """确定性 LLM 替身：先发一次工具调用，再给最终回答。

    刻意**不接受** base_url、不持有 API key：结构上不可能出网。
    """

    def __init__(self, tool_name: str, arguments: dict[str, Any]):
        self._tool_name = tool_name
        self._arguments = arguments
        self._turn = 0

    def _next(self) -> tuple[str, list[dict[str, Any]] | None]:
        self._turn += 1
        if self._turn == 1:
            return "", [
                {
                    "id": "tc-hitl-1",
                    "name": self._tool_name,
                    "arguments": json.dumps(self._arguments, ensure_ascii=False),
                }
            ]
        return "已为您处理。", None

    async def async_invoke(self, messages, timeout=None, tools=None):
        content, tool_calls = self._next()
        return type("_R", (), {"content": content, "tool_calls": tool_calls})()

    async def async_invoke_stream(self, messages, timeout=None):
        content, _ = self._next()
        if content:
            yield content


class _RouterLLM:
    async def async_invoke(self, messages, timeout=None, tools=None):
        return type(
            "_R", (), {"content": '{"query_type":"billing","confidence":0.95}', "tool_calls": None}
        )()

    async def async_invoke_stream(self, messages, timeout=None):
        yield '{"query_type":"billing","confidence":0.95}'


async def _drain_graph(container, *, thread_id: str) -> dict[str, Any]:
    """跑完真实图一次，返回合并后的关键图状态 + 是否发生 interrupt。"""
    observed: dict[str, Any] = {}

    async def _noop_cb(event):
        return None

    from core.streaming_context import reset_stream_callback, set_stream_callback

    token = set_stream_callback(_noop_cb)
    try:
        stream = container.graph_app.astream(
            {
                "session_id": thread_id,
                "customer_query": "我要退款，订单号 HITL-E2E-1，金额 99 元",
                "user_id": "hitl-e2e-user",
                "trace_id": "hitl-e2e",
            },
            config={"configurable": {"thread_id": thread_id}},
            stream_mode="updates",
        )
        async for update in stream:
            for node, value in update.items():
                if node == "__interrupt__":
                    observed["__interrupt__"] = value
                elif isinstance(value, dict):
                    observed.update(value)
    finally:
        reset_stream_callback(token)
    return observed


def _prepare(container, tool_name: str, arguments: dict[str, Any]) -> None:
    from tools.hitl_staging_tools import register_hitl_staging_tools

    register_hitl_staging_tools(container.tool_registry)
    llm = _ScriptedLLM(tool_name, arguments)
    container.llm = llm
    # Agent 在构造期就捕获了 container.llm，因此必须在 initialize() 之后重新注入。
    # 忘了这一步会让 harness「看起来在跑」但实际走的是模板兜底 —— 这是本模块
    # 最早的一个静默失败模式。
    for agent in (container.agents_dict or {}).values():
        agent.llm = llm
    if getattr(container, "response_agent", None) is not None:
        container.response_agent.llm = llm
    if getattr(container, "router", None) is not None:
        container.router.llm = _RouterLLM()

    orchestrator = getattr(container, "orchestrator", None)
    if orchestrator is not None:
        # 固定走 react：只有 react 模式持有 tool_registry，工具循环只在这里。
        orchestrator.select_mode_name = lambda *a, **k: "react"


@pytest.fixture
def hitl_env(monkeypatch):
    """治理**已开启**且规则有效（本轮新增的 fail-closed 校验要求如此）。"""
    import core.config as config_module

    monkeypatch.setattr(config_module, "HITL_ENABLED", True)
    monkeypatch.setattr(
        config_module, "HITL_HIGH_RISK_TOOLS", "staging_refund,staging_order_change"
    )
    monkeypatch.setattr(config_module, "HITL_HIGH_AMOUNT_THRESHOLD", 0.0)
    monkeypatch.setattr(config_module, "TOOL_EXECUTION_TIMEOUT_SECONDS", 30.0)
    return config_module


class TestHitlGateOnRealGraph:
    async def test_high_risk_action_is_deferred_and_graph_interrupts(self, hitl_env):
        """端到端：摘出 → 透传到图状态 → ``human_approval_gate`` 挂起。

        这是本轮修复的核心断言。修复前 ``pending_actions`` 停在 Agent 内部，
        闸门节点从未被触达。
        """
        from core.container import ServiceContainer
        from runtime.context import reset_run_context, set_run_context
        from tools.hitl_staging_tools import reset_staging_ledger, staging_call_count

        container = ServiceContainer()
        await container.initialize()
        try:
            _prepare(container, "staging_refund", {"order_id": "HITL-E2E-1", "amount": 99})
            reset_staging_ledger()

            thread_id = "hitl-e2e-thread"
            tokens = set_run_context("hitl-e2e-run", thread_id)
            try:
                observed = await _drain_graph(container, thread_id=thread_id)
            finally:
                reset_run_context(tokens)

            assert observed.get("pending_actions"), (
                "HIGH 风险动作没有被透传到图状态 —— 闸门节点将永远不会被触达"
                "（ReActMode 丢弃 pending_actions 的历史 bug）"
            )
            assert "__interrupt__" in observed, (
                "图没有挂在 __interrupt__ 上 —— 审批请求从未发出，" "被摘出的高风险动作会被静默丢弃"
            )
            assert (
                staging_call_count("HITL-E2E-1") == 0
            ), "审批挂起期间副作用已经发生 —— 这正是审批要防的事"
        finally:
            await container.close()

    async def test_read_only_tool_is_not_gated(self, hitl_env):
        """只读工具**不得**被闸门拦下 —— 治理误伤会把普通查询拖进人工流程。"""
        from core.container import ServiceContainer
        from runtime.context import reset_run_context, set_run_context
        from tools.hitl_staging_tools import reset_staging_ledger, staging_call_count

        container = ServiceContainer()
        await container.initialize()
        try:
            _prepare(container, "staging_readonly_lookup", {"order_id": "HITL-E2E-RO"})
            reset_staging_ledger()

            thread_id = "hitl-e2e-thread-ro"
            tokens = set_run_context("hitl-e2e-run-ro", thread_id)
            try:
                observed = await _drain_graph(container, thread_id=thread_id)
            finally:
                reset_run_context(tokens)

            assert "__interrupt__" not in observed, "只读工具被错误地拦进了审批流程"
            assert staging_call_count("HITL-E2E-RO") == 0  # 只读工具不进 staging 账本
            assert observed.get("pending_actions") in (None, []), "只读工具产生了待审批动作"
        finally:
            await container.close()

    async def test_without_run_context_approval_is_unreachable_but_write_is_refused(self, hitl_env):
        """无 run 上下文：闸门**明确不拦**（拦了也无法挂起/恢复）。

        该路径的治理边界是 ``ToolRegistry`` **拒绝执行**无治理的写操作，
        而不是审批。两条机制各司其职，不得互相冒充 —— 断言的是「拒绝」而不是
        「悄悄放行」。
        """
        from core.container import ServiceContainer
        from tools.hitl_staging_tools import register_hitl_staging_tools, staging_ledger

        container = ServiceContainer()
        await container.initialize()
        try:
            register_hitl_staging_tools(container.tool_registry)
            # 直接打统一执行边界（与图无关），断言治理判定本身。
            message = await container.tool_registry.execute_raw(
                "staging_refund", {"order_id": "HITL-E2E-NOCTX", "amount": 10}
            )
            assert "需要持久化 Run 上下文" in str(
                message
            ), f"无 run 上下文的写操作没有被拒绝：{message!r}"
            assert staging_ledger() == {}, "被拒绝的写操作仍然产生了副作用"
        finally:
            await container.close()


class TestReactModePropagatesPendingActions:
    """纯单元层：锁住「模式返回值必须带 pending_actions」这条契约。

    为什么要有这个「看起来很笨」的断言：真实图那条测试要拉起整个容器、
    跑完整图，慢且重；而这次的 bug 恰恰是一个**返回值漏字段**。这条断言 5 毫秒
    就跑完，且能在 CI 的 unit lane 里挡住同类回归。
    """

    def test_react_mode_returns_pending_actions_key(self):
        import inspect

        from collaboration.modes import ReActMode

        source = inspect.getsource(ReActMode.execute)
        assert '"pending_actions"' in source, (
            "ReActMode.execute 的返回值里没有 pending_actions —— "
            "HIGH 风险动作会停在 Agent 内部，既不执行也不审批（静默丢弃）"
        )

    def test_all_collaboration_modes_are_accounted_for(self):
        """每个协作模式的正常返回路径都必须透传 pending_actions。

        目前只有 ``react`` 模式持有 tool_registry，因此只有它能摘出动作；
        但这是**当前**的实现事实，不是契约。把「每个模式都要透传」写成断言，
        将来新增带工具的模式时不会重蹈覆辙。
        """
        import inspect

        from collaboration import modes

        mode_classes = [
            getattr(modes, name)
            for name in (
                "SequentialMode",
                "ParallelMode",
                "ConsultationMode",
                "HierarchicalMode",
                "ReActMode",
            )
        ]
        for cls in mode_classes:
            source = inspect.getsource(cls.execute)
            assert '"pending_actions"' in source, (
                f"{cls.__name__}.execute 没有透传 pending_actions；"
                "若该模式将来能触发工具调用，高风险动作会被静默丢弃"
            )
