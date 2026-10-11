"""工具执行层可靠性契约（超时 / 重试 / 熔断 / 并行 的**真实**边界）。

这份文件最重要的部分不是它断言了什么，而是它**断言了什么不存在**。

背景
----
LLM 客户端（``llm/client.py``）有重试、指数退避与熔断器，很容易让人以为工具层
也有一整套。事实是：工具层的熔断器与重试**从来不存在**，只有 MCP 适配器有
per-call timeout。本文件把这条边界钉死：

- 超时：**已实现**（``ToolRegistry._call_handler``，``TOOL_EXECUTION_TIMEOUT_SECONDS``），
  两条执行分支（只读直调 / 幂等 ledger 闭包）都必须受它约束；
- 重试：**不存在**。工具失败不以退避重试的形式回到 LLM 或 executor；`RETRY_MAX_ATTEMPTS`
  是 LLM 客户端的配置，与工具无关；
- 熔断：**不存在**。`CircuitBreaker` 只保护 LLM；
- 并行：**不存在**。``agents/base_agent.py`` 的工具循环是顺序 ``for``。

把「不存在」写成断言，是为了防止下一轮有人看到 LLM 有、就顺手给工具也补一个
「同名同参」的旋钮，然后文档里就会出现一句无法验证的「工具层支持重试」。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
import uuid

import pytest

from core.config import validate_tool_settings
from tools.tool_registry import ToolRegistry


def _registry(*tools) -> ToolRegistry:
    """``_registry(("name", handler, side_effect=True), ...)``"""
    registry = ToolRegistry()
    for entry in tools:
        name, handler = entry[0], entry[1]
        kwargs = entry[2] if len(entry) > 2 else {}
        registry.register(
            name=name,
            description=f"test tool {name}",
            parameters={"type": "object", "properties": {}},
            handler=handler,
            **kwargs,
        )
    return registry


@pytest.fixture
def _tmp_side_effect_store():
    """把幂等 ledger 指向一个**由当前模型元数据新建**的临时 SQLite。

    默认 store 用进程级的 ``get_db_session``，指向开发用的 ``data/csai.db``。
    那张表的列取决于本机跑过哪些 migration：全量测试里它可能缺少
    ``tool_side_effects.claim_owner``（较新 migration 才加的列），于是相关用例会在
    一个与被测语义无关的 SQL 错误上失败 —— 一个只在特定环境下出现的假失败。
    单元测试不依赖环境状态，它依赖模型。
    """
    from runtime import side_effects
    from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory

    factory, engine, path = make_sqlite_session_factory()
    original = side_effects._default_store
    side_effects._default_store = side_effects.SideEffectStore(factory)
    try:
        yield
    finally:
        side_effects._default_store = original
        dispose(engine, path)


# ─────────────────────────── 超时（已实现） ───────────────────────────


class TestNativeToolTimeout:
    """原生工具此前**完全裸跑** —— 一个卡住的 ERP 连接会拖住整条 run。"""

    async def test_slow_tool_is_aborted_and_returns_explainable_error(self, monkeypatch):
        monkeypatch.setattr("core.config.TOOL_EXECUTION_TIMEOUT_SECONDS", 0.05)
        started = time.perf_counter()

        async def _hang(arguments):
            await asyncio.sleep(30)
            return "never"

        registry = _registry(("slow_query", _hang))
        result = await registry.execute_raw("slow_query", {})
        elapsed = time.perf_counter() - started

        assert "超时" in result, f"超时没有返回可解释的错误：{result!r}"
        assert elapsed < 5, f"超时未生效，调用耗时 {elapsed:.1f}s"

    async def test_fast_tool_is_unaffected(self, monkeypatch):
        monkeypatch.setattr("core.config.TOOL_EXECUTION_TIMEOUT_SECONDS", 30.0)

        async def _fast(arguments):
            await asyncio.sleep(0.01)
            return "ok-result"

        registry = _registry(("fast_query", _fast))
        assert await registry.execute_raw("fast_query", {}) == "ok-result"

    async def test_zero_timeout_disables_the_guard(self, monkeypatch):
        """显式设 0 = 不加超时包装（保留历史行为），而不是静默套一个默认超时。"""
        monkeypatch.setattr("core.config.TOOL_EXECUTION_TIMEOUT_SECONDS", 0.0)

        async def _slow_but_finite(arguments):
            await asyncio.sleep(0.2)
            return "done-eventually"

        registry = _registry(("slow_but_ok", _slow_but_finite))
        assert await registry.execute_raw("slow_but_ok", {}) == "done-eventually"

    async def test_timeout_also_covers_the_idempotent_side_effect_path(
        self, monkeypatch, _tmp_side_effect_store
    ):
        """副作用工具走 ledger 闭包，**也**必须受同一个超时约束。

        否则「只读工具有超时、写工具没有」就成了一句需要记住的例外，而它恰恰是
        最需要超时的那一类（ERP 写接口往往比查询更容易挂住）。
        """
        monkeypatch.setattr("core.config.TOOL_EXECUTION_TIMEOUT_SECONDS", 0.05)
        calls: list[str] = []

        async def _slow_write(arguments):
            calls.append("invoked")
            await asyncio.sleep(30)
            return "never"

        registry = _registry(("slow_write", _slow_write, {"side_effect": True}))

        from runtime.context import reset_run_context, set_run_context
        from tools.tool_registry import ToolExecutionTimeout

        # operation_key = run_id:call_id 落在持久化的 tool_side_effects 表里。
        # 用固定 run_id 会命中上一次执行留下的 SUCCEEDED 记录并直接返回缓存 ——
        # 测试会「通过」，但它根本没验证到超时。
        run_id = f"run-timeout-{uuid.uuid4().hex[:12]}"
        tokens = set_run_context(run_id, "thread-1")
        try:
            # 必须冒泡：若转成字符串返回，ledger 会把这次「结果未知」的写操作记成
            # SUCCEEDED 并缓存 —— 重投递时直接返回该字符串，写操作再也不会执行，
            # 而 run 表面成功。
            with pytest.raises(ToolExecutionTimeout):
                await registry.execute_raw("slow_write", {"amount": 1}, tool_call_id="call-1")
        finally:
            reset_run_context(tokens)
        assert calls == ["invoked"], "副作用 handler 未被调用，超时测试本身失效"

    async def test_timeout_emits_a_metric(self, monkeypatch):
        """超时必须可观测 —— 否则「工具被卡住」这件事没有任何信号。"""
        monkeypatch.setattr("core.config.TOOL_EXECUTION_TIMEOUT_SECONDS", 0.05)

        async def _hang(arguments):
            await asyncio.sleep(30)

        registry = _registry(("hang_query", _hang))
        await registry.execute_raw("hang_query", {})

        from prometheus_client import REGISTRY

        value = REGISTRY.get_sample_value("tool_execution_timeout_total")
        if value is None:  # prometheus_client 未安装 -> 指标降级为 no-op
            pytest.skip("prometheus_client 未安装，指标为 no-op")
        assert value >= 1.0, "超时未记录到 tool_execution_timeout_total"


class TestToolTimeoutConfigContract:
    def test_timeout_must_be_smaller_than_task_time_limit(self):
        """工具超时要能**先**于 run 级 time limit 触发，否则局部降级形同虚设。"""
        assert (
            validate_tool_settings(tool_timeout_seconds=30.0, task_time_limit_seconds=180.0) == []
        )
        assert validate_tool_settings(
            tool_timeout_seconds=300.0, task_time_limit_seconds=180.0
        ), "工具超时 > task time limit 竟然通过校验（局部降级永远不会发生）"
        assert validate_tool_settings(
            tool_timeout_seconds=180.0, task_time_limit_seconds=180.0
        ), "工具超时 == task time limit 竟然通过校验（谁先到期不确定）"

    def test_zero_is_an_explicit_opt_out(self):
        assert validate_tool_settings(tool_timeout_seconds=0.0, task_time_limit_seconds=180.0) == []


# ─────────────────── 断言「不存在」的边界（防夸大声明） ───────────────────


class TestAbsentToolLayerFeatures:
    """这些能力**不存在**。断言它们不存在，比在文档里写「暂不支持」更可靠。"""

    def test_tool_registry_has_no_retry_loop(self):
        """工具执行**没有**重试 / 退避。``RETRY_MAX_ATTEMPTS`` 是 LLM 客户端的配置。"""
        source = inspect.getsource(ToolRegistry)
        for forbidden in ("RETRY_MAX_ATTEMPTS", "backoff", "max_attempts", "for attempt in"):
            assert forbidden not in source, (
                f"ToolRegistry 出现了 {forbidden!r} —— 工具层重试是新增能力，"
                "必须同时补测试、配置项与文档，不能静默出现"
            )

    def test_tool_registry_has_no_circuit_breaker(self):
        """熔断器只保护 LLM（``core.monitoring.CircuitBreaker``），不保护工具。"""
        source = inspect.getsource(ToolRegistry)
        assert (
            "circuit" not in source.lower()
        ), "ToolRegistry 引用了熔断器 —— 工具级熔断是新增能力，需要配套验证"

    def test_agent_tool_loop_is_sequential_not_parallel(self):
        """``base_agent`` 的工具循环是顺序 ``for``，LLM 一次返回多个 tool_call 时
        没有并行收益。改之前必须先补「副作用工具串行」的语义说明。"""
        from agents import base_agent

        source = inspect.getsource(base_agent.BaseAgent._process_with_tools)
        assert "asyncio.gather(" not in source, (
            "工具循环出现了 asyncio.gather —— 并行工具调用是新增能力；"
            "必须先确认副作用工具仍然串行（ledger 的 operation_key 语义依赖调用序）"
        )

    def test_llm_circuit_breaker_is_not_a_tool_circuit_breaker(self):
        """显式区分：``CircuitBreaker`` 挂在 LLM 客户端上，与工具无关。"""
        from llm import client as llm_client

        source = inspect.getsource(llm_client)
        assert "CircuitBreaker" in source, "LLM 客户端的熔断器不见了（LLM 侧能力回退）"
        # 反向：ToolRegistry 不引用它
        assert "CircuitBreaker" not in inspect.getsource(ToolRegistry)


class TestToolTimeoutIsRetryableAtRunLevel:
    """超时必须被 runtime 的错误分类识别为**可重试**，否则 run 会直接 FAILED。

    分类按**属性**（``is_tool_timeout``）而不是 isinstance，这样 ``runtime`` 不必
    import ``tools`` —— 两者是独立包边界，反向依赖会把工具层锁死在运行时里。
    """

    def test_classified_as_timeout_and_retryable(self):
        from runtime.errors import TIMEOUT, classify_exception, is_retryable
        from tools.tool_registry import ToolExecutionTimeout

        exc = ToolExecutionTimeout("slow_write", 30.0, "超时")
        assert classify_exception(exc) == TIMEOUT
        assert (
            is_retryable(classify_exception(exc)) is True
        ), "工具超时被判为不可重试 —— run 会直接 FAILED，而结果其实是未知的"

    def test_runtime_does_not_import_tools(self):
        """包边界：``runtime`` 不得反向依赖 ``tools``。"""
        import pathlib

        from runtime.errors import classify_exception

        runtime_pkg = pathlib.Path(inspect.getfile(classify_exception)).parent
        offenders = [
            p.name
            for p in runtime_pkg.glob("*.py")
            if re.search(r"^\s*(from|import)\s+tools\b", p.read_text(encoding="utf-8"), re.M)
        ]
        assert not offenders, f"runtime 反向 import tools，破坏包边界：{offenders}"
