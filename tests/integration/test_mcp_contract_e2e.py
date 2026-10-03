"""MCP 端到端契约证据：registry → adapter → 本地 fake server → result → policy/telemetry。

这条测试驱动的**每一段都是真的**，没有一段是 mock 出来的：

===========================  ====================================================
环节                          实现
===========================  ====================================================
ToolRegistry                 本仓 ``tools/tool_registry.py`` 真实实例
MCP adapter                  本仓 ``tools/mcp_adapter.py`` 真实实现
client 侧协议实现              **官方** ``mcp`` SDK（``mcp.client.stdio`` + ``ClientSession``）
server 侧协议对端              本仓 ``tests/integration/fake_mcp_server.py``
                             （MCP stdio wire protocol，换行分隔 JSON-RPC）
传输                          真实子进程 + 真实 stdin/stdout 管道
===========================  ====================================================

**不依赖任何外部公开 MCP 服务。** 唯一的外部依赖是本仓自己写的 fake server 文件，
因此这个契约在离线 CI 里可复现，也不会因为第三方服务限流/改协议而变红。

覆盖的 5 类工具行为（对应 MCP 接入必须回答的 5 个问题）：

1. **safe read** —— 只读工具能否端到端取回结果，且结果确定；
2. **high-risk side effect** —— 写操作工具在注册阶段是否被 fail-closed 拦住；
3. **timeout** —— 超时是否稳定收敛到 ``MCPTimeoutError``；
4. **oversized** —— 请求侧 payload 上限是否真的生效（并如实刻画响应侧缺口）；
5. **failing** —— server 报告的失败是否稳定收敛到 ``MCPToolExecutionError``。

另外断言 policy（风险等级 / 审批闸门）与 telemetry（Prometheus 标签值）两条腿。

证据边界见 ``docs/reference/current-state.md`` 的 MCP 段落与
``docs/interview/failure-and-tradeoffs.md`` §7。**真实第三方 MCP server、生产连通性
与写操作 MCP 工具都是 ``NOT_VERIFIED`` / ``NOT_IMPLEMENTED``**，本文件不声称覆盖。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from core.hitl.risk import RiskLevel
from tools.mcp_adapter import (
    MCPPayloadTooLargeError,
    MCPServerConfig,
    MCPTimeoutError,
    MCPToolAdapter,
    MCPToolExecutionError,
    MCPUnauthorizedToolError,
    build_mcp_adapters,
    register_mcp_tools,
)
from tools.tool_registry import SOURCE_MCP, SOURCE_NATIVE, ToolRegistry

pytestmark = pytest.mark.integration

#: 官方 SDK 是 client 侧协议的**唯一**实现，缺了它这条契约无从谈起。
#: 用 ``importorskip`` 而不是静默通过：跳过的证据不是证据。
pytest.importorskip("mcp", reason="官方 mcp SDK 未安装：MCP 端到端契约无法验证")

FAKE_SERVER = Path(__file__).with_name("fake_mcp_server.py")

#: fake server 暴露的全部工具名（含故意不在 allowlist 里的那个）。
ALL_FAKE_TOOLS = (
    "get_product_info",
    "issue_refund",
    "slow_lookup",
    "bulk_dump",
    "flaky_lookup",
    "secret_admin_tool",
)

#: ``get_product_info`` 的**精确**确定性输出。写死在这里有两个作用：既验证结果
#: 正确，也验证 fake server 确实确定性（同一个 sku 必须每次得到同一串字节）。
EXPECTED_READ_RESULT = '{"in_stock": true, "name": "测试商品 SKU-1", "price": 99.0, "sku": "SKU-1"}'


def _counter(name: str, **labels: str) -> float:
    """读一个 Prometheus counter 的当前值（未自增时为 0.0）。"""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels) or 0.0


def _config(
    *,
    name: str = "fake",
    risk_level: Any = "low",
    timeout_seconds: float = 5.0,
    max_payload_bytes: int = 32768,
    allowed_tools: tuple[str, ...] = ALL_FAKE_TOOLS,
) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=(str(FAKE_SERVER),),
        allowed_tools=allowed_tools,
        timeout_seconds=timeout_seconds,
        max_payload_bytes=max_payload_bytes,
        risk_level=risk_level,
    )


@pytest.fixture
async def adapter_factory():
    """建 adapter 并保证 teardown（否则 stdio 子进程会泄漏成孤儿进程）。"""
    created: list[MCPToolAdapter] = []

    def _make(**kwargs: Any) -> MCPToolAdapter:
        adapter = MCPToolAdapter(_config(**kwargs))
        created.append(adapter)
        return adapter

    yield _make

    for adapter in created:
        await adapter.close()


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


# ===== 1. safe read：完整链路 + 确定性 =====


class TestSafeReadContract:
    async def test_read_tool_round_trips_through_the_whole_chain(self, adapter_factory, registry):
        """ToolRegistry.execute → adapter → 官方 SDK client → fake server → 结果。"""
        adapter = adapter_factory()
        registered = await register_mcp_tools(registry, adapter)
        assert "mcp__fake__get_product_info" in registered

        result = await registry.execute("mcp__fake__get_product_info", {"sku": "SKU-1"})
        assert result == EXPECTED_READ_RESULT

    async def test_read_result_is_deterministic_across_calls(self, adapter_factory, registry):
        """同样的输入必须得到同样的字节 —— 否则它不是可断言的契约。"""
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)
        first = await registry.execute("mcp__fake__get_product_info", {"sku": "SKU-1"})
        second = await registry.execute("mcp__fake__get_product_info", {"sku": "SKU-1"})
        assert first == second == EXPECTED_READ_RESULT

    async def test_mcp_tools_are_namespaced_and_exposed_to_function_calling(
        self, adapter_factory, registry
    ):
        """Agent 侧只看统一 ToolDefinition：命名空间化 + 进入 get_openai_tools。"""
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)

        names = registry.list_tools()
        assert "mcp__fake__get_product_info" in names
        # 命名空间化后不得与任何 native 工具重名
        assert all(n.startswith("mcp__fake__") for n in names)

        fc_names = {t["function"]["name"] for t in registry.get_openai_tools()}
        assert "mcp__fake__get_product_info" in fc_names
        # schema 被归一化成 JSON Schema object，LLM 才能据此生成参数
        params = next(
            t["function"]["parameters"]
            for t in registry.get_openai_tools()
            if t["function"]["name"] == "mcp__fake__get_product_info"
        )
        assert params["type"] == "object"
        assert "sku" in params["properties"]

    async def test_source_labels_separate_mcp_from_native(self, adapter_factory, registry):
        """source 让「工具来自外部进程」在注册表层面可见（纯可观测性）。"""

        async def _native(arguments: dict) -> str:
            return "native"

        registry.register(
            name="native_readonly",
            description="native 只读工具",
            parameters={"type": "object", "properties": {}},
            handler=_native,
        )
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)

        assert registry.source_for("native_readonly") == SOURCE_NATIVE
        assert registry.source_for("mcp__fake__get_product_info") == SOURCE_MCP
        assert registry.tools_by_source(SOURCE_MCP) == registry.tools_by_source("mcp")
        assert "native_readonly" in registry.tools_by_source(SOURCE_NATIVE)
        assert "native_readonly" not in registry.tools_by_source(SOURCE_MCP)


# ===== 2. high-risk side effect：fail-closed policy =====


class TestHighRiskSideEffectPolicy:
    async def test_write_declared_server_registers_nothing(self, adapter_factory, registry):
        """server 声明 risk_level=high → 一个工具都不注册（只注册显式 low）。

        这是本仓对「外部不可信工具」的核心防线：写操作 MCP 工具**不进注册表**，
        因此不会出现在 Agent 的 Function Calling 列表里，也永远到不了 HITL 闸门。
        """
        adapter = adapter_factory(risk_level="high")
        before = _counter("mcp_tool_register_total", server="fake", outcome="skipped_not_low_risk")
        registered = await register_mcp_tools(registry, adapter)

        assert registered == []
        assert registry.list_tools() == []
        assert registry.get_openai_tools() == []
        after = _counter("mcp_tool_register_total", server="fake", outcome="skipped_not_low_risk")
        assert after - before == len(ALL_FAKE_TOOLS)

    async def test_write_tool_is_absent_from_function_calling_surface(
        self, adapter_factory, registry
    ):
        """即使 LLM「想」调用，也没有任何可达路径 —— 名字根本不存在。"""
        adapter = adapter_factory(risk_level="high")
        await register_mcp_tools(registry, adapter)
        assert "mcp__fake__issue_refund" not in registry.list_tools()
        assert "mcp__fake__issue_refund" not in registry.tools_by_source(SOURCE_MCP)
        # registry.execute 对未知工具返回错误字符串而不是抛异常
        out = await registry.execute("mcp__fake__issue_refund", {"order_id": "O-1"})
        assert "不存在" in out

    async def test_low_declared_server_still_exposes_the_write_tool(
        self, adapter_factory, registry
    ):
        """**已知缺口（NOT_IMPLEMENTED 的响应侧/逐工具风险）** —— 如实刻画，不掩盖。

        ``MCPServerConfig.risk_level`` 是**按 server** 声明的，``discover_tools`` 把它
        原样赋给该 server 的**每一个**工具。因此当一个 server 被显式声明为 ``low`` 时，
        它暴露的 ``issue_refund`` 会被当成只读工具注册下来（risk_level="low"）。

        也就是说：「只注册显式 low」这道防线的强度**取决于操作者是否把 server 正确
        声明为 low**，而 server 本身是不可信输入。本仓当前没有逐工具的风险声明 /
        校验，也没有在注册时交叉核对工具语义。

        这条断言的作用是**锁住当前真实行为**：一旦将来补上逐工具风险治理，这条测试
        会红，从而提醒同步更新设计文档，而不是让缺口悄悄固化。
        """
        adapter = adapter_factory(risk_level="low")
        registered = await register_mcp_tools(registry, adapter)

        assert "mcp__fake__issue_refund" in registered
        assert registry.risk_level_for("mcp__fake__issue_refund") == "low"
        assert registry.is_side_effect("mcp__fake__issue_refund") is False


# ===== 3. timeout =====


class TestTimeoutContract:
    async def test_timeout_raises_mcp_timeout_deterministically(self, adapter_factory, registry):
        """慢工具必须**稳定**收敛到 MCPTimeoutError。

        稳定性来自两侧的确定性设计：fake server 固定 sleep 30s（远大于 timeout），
        且 SDK 的 read timeout 被设成外层超时的 2 倍（``_SDK_READ_TIMEOUT_SLACK``），
        所以外层第一道防线必定先到期 —— 异常类型不会在两个超时来源之间摇摆。
        """
        adapter = adapter_factory(timeout_seconds=0.5)
        await register_mcp_tools(registry, adapter)

        for _ in range(3):
            with pytest.raises(MCPTimeoutError):
                await adapter.invoke("slow_lookup", {"q": "x"})

    async def test_timeout_increments_timeout_status_telemetry(self, adapter_factory):
        adapter = adapter_factory(timeout_seconds=0.5)
        before = _counter(
            "mcp_tool_call_total", server="fake", tool="slow_lookup", status="timeout"
        )
        with pytest.raises(MCPTimeoutError):
            await adapter.invoke("slow_lookup", {"q": "x"})
        after = _counter("mcp_tool_call_total", server="fake", tool="slow_lookup", status="timeout")
        assert after - before == 1

    @pytest.mark.timeout(60)
    async def test_adapter_closes_cleanly_after_a_timeout(self, adapter_factory):
        """超时之后 adapter 必须能干净关闭（不能卡死，也不能泄漏子进程）。

        这条不是凑数：``slow_lookup`` 会让 server 挂 30s，而 session 的 teardown 要
        穿过 SDK 的 anyio cancel scope ——「超时后关不掉」是本仓真实踩过的一类生命周期
        缺陷（cancel scope 跨 task 退出会让 close 静默抛 RuntimeError）。

        已知边界（本条**不**声称覆盖）：

        - SDK 的 ``BaseSession.__aexit__`` 在**超时之后**的 teardown 中会取消调用方
          task。若调用方把 ``close()`` 包进 ``asyncio.wait_for``（后者会自己建 task），
          取消会穿过 ``wait_for`` 冒回调用方 —— 这是 ``mcp==1.27.2`` 的 SDK 行为。
          ``McpSdkClient.close`` 因此**显式吞掉 CancelledError**：cleanup 是
          best-effort 的，绝不能把「关一个 MCP session」升级成「整个进程 shutdown 失败」。
          本仓容器（``core/container.py::_close_mcp_tools``）直接 ``await close()``，
          不套 ``wait_for``，因此这条路径是安全的。
        - 挂死保护用 pytest 的 ``timeout`` marker，而不是在测试里套 ``wait_for`` ——
          后者恰恰是上面那个坑的触发方式。
        """
        adapter = adapter_factory(timeout_seconds=0.5)
        await adapter.connect()
        with pytest.raises(MCPTimeoutError):
            await adapter.invoke("slow_lookup", {"q": "x"})
        # 不抛异常即通过；若 close 挂死，timeout marker 会让本条失败而不是挂住整个套件。
        await adapter.close()


# ===== 4. oversized =====


class TestOversizedContract:
    async def test_oversized_request_payload_rejected_before_send(self, adapter_factory):
        """请求侧上限是**真正生效**的防线：超限参数在发出前就被拒。

        注意限制的是**发出去**的请求（``max_payload_bytes``），不是返回值。
        """
        adapter = adapter_factory(max_payload_bytes=1024)
        with pytest.raises(MCPPayloadTooLargeError):
            await adapter.invoke("bulk_dump", {"pad": "x" * 4096})

    async def test_oversized_request_payload_telemetry(self, adapter_factory):
        adapter = adapter_factory(max_payload_bytes=1024)
        before = _counter(
            "mcp_tool_error_total", server="fake", tool="bulk_dump", reason="payload_too_large"
        )
        with pytest.raises(MCPPayloadTooLargeError):
            await adapter.invoke("bulk_dump", {"pad": "x" * 4096})
        after = _counter(
            "mcp_tool_error_total", server="fake", tool="bulk_dump", reason="payload_too_large"
        )
        assert after - before == 1

    async def test_oversized_result_passes_through_unbounded(self, adapter_factory, registry):
        """**已知缺口**：响应体积当前**不受任何限制** —— 如实刻画，不掩盖。

        ``MCPToolAdapter.invoke`` 只在**发送前**检查 payload 大小，返回值直接经
        ``_normalize_result`` 变成 Agent 上下文里的一段文本。一个恶意的 / 失控的
        MCP server 可以返回任意大的结果并把它灌进上下文与 Token 预算。

        本仓的兜底不在适配器里，而在下游：``core/tool_result_*`` 的 Tool Result
        Context Budget（截断 / 压缩 / offload）。也就是说当前依赖的是**通用**防线，
        而不是 MCP 边界自身的限制。

        与上一条同理，这条断言锁住当前真实行为：补上响应侧上限后它会红。
        """
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)

        big = await registry.execute("mcp__fake__bulk_dump", {"size": 200_000})
        assert len(big) == 200_000  # 全量透传，没有被适配器截断


# ===== 5. failing =====


class TestFailingToolContract:
    async def test_server_reported_failure_raises_tool_execution_error(self, adapter_factory):
        """server 明确说 isError → MCPToolExecutionError(reason="tool_error")。"""
        adapter = adapter_factory()
        with pytest.raises(MCPToolExecutionError) as excinfo:
            await adapter.invoke("flaky_lookup", {"code": "E_DOWNSTREAM"})
        assert excinfo.value.reason == "tool_error"
        assert "E_DOWNSTREAM" in str(excinfo.value)

    async def test_failing_tool_error_telemetry_keeps_reason(self, adapter_factory):
        adapter = adapter_factory()
        before = _counter(
            "mcp_tool_error_total", server="fake", tool="flaky_lookup", reason="tool_error"
        )
        with pytest.raises(MCPToolExecutionError):
            await adapter.invoke("flaky_lookup", {"code": "E_DOWNSTREAM"})
        after = _counter(
            "mcp_tool_error_total", server="fake", tool="flaky_lookup", reason="tool_error"
        )
        assert after - before == 1

    async def test_via_registry_failure_degrades_to_user_safe_message(
        self, adapter_factory, registry
    ):
        """经 ToolRegistry 执行时，MCP 异常被降级为一句用户可读的话，而不是把
        内部异常类型 / 远端错误细节抛进 Agent 上下文。"""
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)
        out = await registry.execute("mcp__fake__flaky_lookup", {"code": "E_DOWNSTREAM"})
        assert out == "工具 'mcp__fake__flaky_lookup' 执行失败，请稍后重试"
        # 远端错误细节不得泄漏到 Agent 可见的返回值里
        assert "E_DOWNSTREAM" not in out


# ===== allowlist（policy 的一环）=====


class TestAllowlistPolicy:
    async def test_unlisted_tool_is_never_registered(self, adapter_factory, registry):
        adapter = adapter_factory(allowed_tools=("get_product_info",))
        registered = await register_mcp_tools(registry, adapter)
        assert registered == ["mcp__fake__get_product_info"]
        assert "mcp__fake__secret_admin_tool" not in registry.list_tools()

    async def test_invoking_unlisted_tool_raises_unauthorized(self, adapter_factory):
        adapter = adapter_factory(allowed_tools=("get_product_info",))
        with pytest.raises(MCPUnauthorizedToolError):
            await adapter.invoke("secret_admin_tool", {})

    async def test_unlisted_tool_name_is_not_used_as_metric_label(self, adapter_factory):
        """被拒绝的远端工具名不得成为 label（否则 server 一改工具名就能无界扩张时序）。"""
        adapter = adapter_factory(allowed_tools=("get_product_info",))
        before_star = _counter(
            "mcp_tool_error_total", server="fake", tool="*", reason="not_allowed"
        )
        with pytest.raises(MCPUnauthorizedToolError):
            await adapter.invoke("secret_admin_tool", {})
        after_star = _counter("mcp_tool_error_total", server="fake", tool="*", reason="not_allowed")
        assert after_star - before_star >= 1

        from prometheus_client import REGISTRY

        leaked = [
            sample
            for metric in REGISTRY.collect()
            for sample in metric.samples
            if sample.name == "mcp_tool_error_total"
            and "secret_admin_tool" in sample.labels.get("tool", "")
        ]
        assert leaked == []


# ===== policy：注册表声明与 HITL 闸门一致 =====


class TestRiskPolicyConsistency:
    async def test_registered_read_tools_are_low_risk_and_never_need_approval(
        self, adapter_factory, registry
    ):
        """只读 MCP 工具显式 low + side_effect=False ⇒ HITL 闸门永不拦它。

        这条是「注册表声明」与「审批策略」的一致性断言：``classify_risk`` 的优先级是
        显式声明 > 工具名白名单 > 金额阈值，所以显式 low 同时也保证了一个带
        ``amount=999999`` 的只读查询不会被金额阈值误判成 HIGH 而白占审批队列。
        """
        from core.hitl.risk import RiskLevel, classify_risk, requires_approval

        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)
        name = "mcp__fake__get_product_info"

        assert registry.risk_level_for(name) == "low"
        assert registry.is_side_effect(name) is False
        assert registry.cache_policy_for(name).enabled is False

        level = classify_risk(
            name, explicit=registry.risk_level_for(name), arguments={"sku": "S", "amount": 999999}
        )
        assert level == RiskLevel.LOW
        assert requires_approval(level) is False

    async def test_unknown_tool_is_not_treated_as_low_risk(self, registry):
        """未注册工具必须返回 None（「不知道」）而不是 low（「安全」）。"""
        assert registry.risk_level_for("mcp__nope__nope") is None
        assert registry.source_for("mcp__nope__nope") is None
        assert registry.tools_by_source("nonexistent-source") == []


class TestDefaultHighRiskPolicy:
    """ "未声明就不是低风险" —— 本仓对外部工具的核心立场。

    漏配 / 配错 ``risk_level`` 的后果必须是「工具不注册」，绝不能是「默认按只读
    放行」。否则一个拼错的配置就能让整台 server 的工具静默拿到 ``risk_level=low``，
    从而绕过 HITL 闸门的显式分级。
    """

    def test_default_risk_level_is_high(self):
        from core.hitl.risk import RiskLevel
        from tools.mcp_adapter import DEFAULT_RISK_LEVEL

        assert DEFAULT_RISK_LEVEL is RiskLevel.HIGH

    def test_unconfigured_server_defaults_to_high(self):
        from core.hitl.risk import RiskLevel
        from tools.mcp_adapter import MCPServerConfig

        cfg = MCPServerConfig(name="s", command="c")
        assert cfg.risk_level is RiskLevel.HIGH

    @pytest.mark.parametrize("bad", ["read", "write", "readonly", "safe", "LOW-ish", "?", ""])
    def test_invalid_risk_level_only_converges_upward(self, bad):
        """非法值只能向上收敛到 HIGH —— 绝不回落成 LOW/MEDIUM。

        特别包含历史词汇 ``read`` / ``write``：它们在 ``RiskLevel`` 里不存在，
        若被当成合法值接住就等于退回"外部工具默认只读"的老行为。
        """
        from core.hitl.risk import RiskLevel
        from tools.mcp_adapter import load_mcp_server_configs

        cfgs = load_mcp_server_configs(
            f'[{{"name":"s","command":"c","allowed_tools":["t"],"risk_level":"{bad}"}}]'
        )
        assert len(cfgs) == 1
        assert cfgs[0].risk_level is RiskLevel.HIGH

    @pytest.mark.parametrize("raw,expected", [("low", "low"), ("LOW", "low")])
    def test_explicit_low_is_honoured(self, raw, expected):
        from tools.mcp_adapter import load_mcp_server_configs

        cfgs = load_mcp_server_configs(
            f'[{{"name":"s","command":"c","allowed_tools":["t"],"risk_level":"{raw}"}}]'
        )
        assert cfgs[0].risk_level.value == expected

    def test_bare_string_low_is_normalised_to_risklevel(self):
        """``RiskLevel`` 是 ``str`` Enum：``"low" == RiskLevel.LOW`` 但不是同一对象。

        若不做归一，下游 ``is not RiskLevel.LOW`` 的身份比较会在传入裸字符串时
        静默失效，把工具误判成"非只读"而全部跳过。
        """
        from core.hitl.risk import RiskLevel
        from tools.mcp_adapter import MCPServerConfig

        assert MCPServerConfig(name="s", command="c", risk_level="low").risk_level is (
            RiskLevel.LOW
        )

    async def test_undeclared_server_registers_nothing(self, adapter_factory, registry):
        adapter = adapter_factory(risk_level="high")
        assert await register_mcp_tools(registry, adapter) == []
        assert registry.list_tools() == []

    async def test_medium_risk_server_registers_nothing(self, adapter_factory, registry):
        adapter = adapter_factory(risk_level="medium")
        assert await register_mcp_tools(registry, adapter) == []
        assert registry.list_tools() == []

    async def test_low_risk_server_registers_its_tools(self, adapter_factory, registry):
        adapter = adapter_factory(risk_level="low")
        registered = await register_mcp_tools(registry, adapter)
        assert registered
        assert registry.risk_level_for(registered[0]) == "low"


class TestServerAnnotationsAreNotTrusted:
    """MCP server 自述的 ``annotations`` 不能覆盖本地风险策略。

    第三方只要在自己的代码里加 ``readOnlyHint: true`` / ``destructiveHint: false``，
    就能"自称安全"。适配器故意不读 annotations，因此风险只由本地配置决定。
    """

    async def test_annotations_do_not_change_the_local_risk_level(self, adapter_factory, registry):
        adapter = adapter_factory(risk_level="high")
        specs = await adapter.discover_tools()
        assert specs, "fake server 至少暴露一个工具，断言才有意义"
        # 前提校验：fake server 确实在 wire 上宣称"只读 / 非破坏性"。
        # 没有这一步，"风险没被降低"可能只是因为 annotations 根本没送到，
        # 测试就变成一条永远为真的空断言。
        raw = await adapter._require_client().list_tools()
        assert any(getattr(t, "annotations", None) for t in raw), (
            "fake server 未发出 annotations：req-6 断言会变成空断言"
        )
        # server 即使自述只读，本地判定仍是 HIGH。
        assert all(s.risk_level is RiskLevel.HIGH for s in specs)
        assert await register_mcp_tools(registry, adapter) == []

    async def test_annotations_do_not_block_an_explicitly_low_server(
        self, adapter_factory, registry
    ):
        """反向：annotations 也不是"放行"的额外门槛之外的干扰项。

        显式声明 ``low`` 的 server 照常注册，证明判定链路只读本地配置。
        """
        adapter = adapter_factory(risk_level="low")
        assert await register_mcp_tools(registry, adapter) != []


# ===== config 层 fail-closed（纯函数，无 IO）=====


class TestConfigFailClosed:
    def test_disabled_by_default(self):
        from core.config import validate_mcp_settings

        assert validate_mcp_settings(enabled=False, servers="") == []

    def test_enabled_without_allowlist_is_rejected(self):
        from core.config import validate_mcp_settings

        errors = validate_mcp_settings(enabled=True, servers="   ")
        assert errors and "MCP_SERVERS" in errors[0]

    @pytest.mark.parametrize(
        "servers",
        ["{not json", '{"name": "x"}', "[]", '[{"transport": "stdio"}]'],
    )
    def test_structurally_invalid_allowlist_is_rejected(self, servers):
        from core.config import validate_mcp_settings

        assert validate_mcp_settings(enabled=True, servers=servers) != []

    def test_valid_allowlist_passes(self):
        from core.config import validate_mcp_settings

        servers = (
            '[{"name":"fake","transport":"stdio","command":"python3",'
            '"args":["-m","srv"],"allowed_tools":["get_product_info"],'
            '"risk_level":"low"}]'
        )
        assert validate_mcp_settings(enabled=True, servers=servers) == []

    def test_missing_risk_level_is_reported_at_startup(self):
        """漏配 risk_level 必须在**启动期**就报出来，而不是等到工具列表为空才发现。"""
        from core.config import validate_mcp_settings

        servers = (
            '[{"name":"fake","transport":"stdio","command":"python3",'
            '"args":["-m","srv"],"allowed_tools":["get_product_info"]}]'
        )
        problems = validate_mcp_settings(enabled=True, servers=servers)
        assert any("未显式声明 risk_level" in p for p in problems), problems

    @pytest.mark.parametrize("bad", ["read", "write", "safe", "???"])
    def test_invalid_risk_level_is_reported_at_startup(self, bad):
        """旧词汇 read/write 与笔误都必须被指出，而不是被静默当成合法值。"""
        from core.config import validate_mcp_settings

        servers = (
            f'[{{"name":"fake","transport":"stdio","command":"python3",'
            f'"args":["-m","srv"],"allowed_tools":[],"risk_level":"{bad}"}}]'
        )
        problems = validate_mcp_settings(enabled=True, servers=servers)
        assert any("risk_level 非法" in p for p in problems), problems

    def test_config_parser_rejects_unknown_transport(self):
        from tools.mcp_adapter import load_mcp_server_configs

        configs = load_mcp_server_configs(
            '[{"name":"x","transport":"carrier-pigeon","command":"c"}]'
        )
        assert configs == []

    def test_build_adapters_skips_disabled_servers(self):
        from tools.mcp_adapter import load_mcp_server_configs

        configs = load_mcp_server_configs(
            '[{"name":"on","command":"c","enabled":true},'
            '{"name":"off","command":"c","enabled":false}]'
        )
        adapters = build_mcp_adapters(configs)
        assert [a.config.name for a in adapters] == ["on"]


# ===== telemetry：happy path 计数 =====


class TestTelemetryContract:
    async def test_successful_call_increments_ok_status(self, adapter_factory, registry):
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)
        before = _counter(
            "mcp_tool_call_total", server="fake", tool="get_product_info", status="ok"
        )
        await registry.execute("mcp__fake__get_product_info", {"sku": "SKU-1"})
        after = _counter("mcp_tool_call_total", server="fake", tool="get_product_info", status="ok")
        assert after - before == 1

    async def test_register_outcome_counter(self, adapter_factory, registry):
        before = _counter("mcp_tool_register_total", server="fake", outcome="registered")
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)
        after = _counter("mcp_tool_register_total", server="fake", outcome="registered")
        assert after - before == len(ALL_FAKE_TOOLS)

    def test_metrics_do_not_use_high_cardinality_labels(self):
        """MCP 指标的 label 只允许 server/tool/status/reason/outcome 这类低基数维度。"""
        from prometheus_client import REGISTRY

        forbidden = {"run_id", "thread_id", "user_id", "query", "session_id", "error_message"}
        offenders = [
            (sample.name, set(sample.labels))
            for metric in REGISTRY.collect()
            if metric.name in {"mcp_tool", "mcp_tool_error", "mcp_tool_register"}
            for sample in metric.samples
            if forbidden & set(sample.labels)
        ]
        assert offenders == []
