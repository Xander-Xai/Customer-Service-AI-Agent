"""MCP 工具适配层的**纯函数**契约单测（不连接任何 server）。

覆盖 PR1 落地的产品代码里所有不需要 MCP server 就能钉死的契约：

  - 工具名命名空间化（Function Calling 长度上限 + 截断不撞名）；
  - ``inputSchema`` 归一化与 fail-closed；
  - ``risk_level`` 只向上收敛（缺失 / 非法 -> HIGH，绝不因配置笔误放行）；
  - ``MCP_SERVERS`` allowlist 解析（整体非法 / 单条非法 / transport 白名单）；
  - 启动期结构校验（``validate_mcp_settings``）；
  - ToolRegistry 的 ``source`` 可观测性维度（不改变执行语义）。

**不在这里测什么**：跨进程 / 传输 / 策略 / 时序的端到端取证属于 PR2，
见 ``tests/integration/test_mcp_contract_e2e.py`` 与
``tests/integration/fake_mcp_server.py``。本文件**不引入** MCP server，
因此不需要 ``mcp`` SDK 即可运行。
"""

from __future__ import annotations

import json

import pytest

from core.config import validate_mcp_settings
from core.hitl.risk import RiskLevel
from tools.mcp_adapter import (
    DEFAULT_RISK_LEVEL,
    FC_NAME_MAX_LEN,
    MCP_NAME_PREFIX,
    MCPConfigurationError,
    MCPInvalidSchemaError,
    build_mcp_adapters,
    load_mcp_server_configs,
    normalize_input_schema,
    qualified_tool_name,
)
from tools.tool_registry import SOURCE_MCP, SOURCE_NATIVE, ToolRegistry

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Tool name namespacing
# ---------------------------------------------------------------------------


class TestQualifiedToolName:
    def test_prefix_is_namespaced_and_stable(self):
        assert qualified_tool_name("catalog", "get_sku") == "mcp__catalog__get_sku"

    def test_illegal_characters_are_sanitized(self):
        name = qualified_tool_name("cat alog", "get/sku")
        assert name == "mcp__cat_alog__get_sku"
        assert all(c.isalnum() or c in "_-" for c in name)

    def test_short_name_is_not_truncated(self):
        assert len(qualified_tool_name("c", "t")) <= FC_NAME_MAX_LEN

    def test_long_name_is_capped_at_function_calling_limit(self):
        name = qualified_tool_name("s" * 40, "t" * 40)
        assert len(name) == FC_NAME_MAX_LEN

    def test_truncation_does_not_collide_across_distinct_tools(self):
        # 截断是「先截断再补 sha256 前 8 位」，因此不同的 (server, tool)
        # 即使共享同一段前缀也不会撞名 —— 否则会静默覆盖掉一个已注册的工具。
        a = qualified_tool_name("s" * 40, "prefix_" + "a" * 40)
        b = qualified_tool_name("s" * 40, "prefix_" + "b" * 40)
        assert a != b

    def test_same_input_is_deterministic(self):
        assert qualified_tool_name("catalog", "get_sku") == qualified_tool_name(
            "catalog", "get_sku"
        )


# ---------------------------------------------------------------------------
# inputSchema normalization (fail closed)
# ---------------------------------------------------------------------------


class TestNormalizeInputSchema:
    def test_missing_schema_becomes_empty_object_schema(self):
        assert normalize_input_schema(None) == {"type": "object", "properties": {}}

    def test_type_defaults_to_object(self):
        out = normalize_input_schema({"properties": {"a": {"type": "string"}}})
        assert out["type"] == "object"
        assert out["properties"] == {"a": {"type": "string"}}

    def test_missing_properties_defaults_to_empty_dict(self):
        assert normalize_input_schema({"type": "object"})["properties"] == {}

    def test_non_object_type_fails_closed(self):
        # 非 object 的 schema 无法保证 Function Calling 侧安全 → 不静默猜测。
        with pytest.raises(MCPInvalidSchemaError):
            normalize_input_schema({"type": "string"})

    def test_non_dict_schema_fails_closed(self):
        with pytest.raises(MCPInvalidSchemaError):
            normalize_input_schema(["not", "a", "dict"])

    def test_non_dict_properties_fails_closed(self):
        with pytest.raises(MCPInvalidSchemaError):
            normalize_input_schema({"type": "object", "properties": ["a"]})


# ---------------------------------------------------------------------------
# Risk policy: converge upward only
# ---------------------------------------------------------------------------


class TestRiskLevelCoercion:
    def test_default_is_high(self):
        assert DEFAULT_RISK_LEVEL is RiskLevel.HIGH

    def test_explicit_low_is_honoured(self):
        cfg = load_mcp_server_configs(_servers(risk_level="low"))
        assert cfg[0].risk_level is RiskLevel.LOW

    def test_whitespace_and_case_are_normalized(self):
        cfg = load_mcp_server_configs(_servers(risk_level="  LoW "))
        assert cfg[0].risk_level is RiskLevel.LOW

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_missing_risk_level_converges_to_high(self, bad):
        raw = json.dumps([{"name": "s", "risk_level": bad}])
        cfg = load_mcp_server_configs(raw)
        assert cfg[0].risk_level is RiskLevel.HIGH

    @pytest.mark.parametrize("bad", ["read", "write", "safe", "lowest", 1, True])
    def test_illegal_risk_level_never_converges_downward(self, bad):
        # 关键安全性质：非法值（含历史词汇 read/write）只向上收敛到 HIGH，
        # 绝不 fail-open 到 LOW/MEDIUM。
        raw = json.dumps([{"name": "s", "risk_level": bad}])
        cfg = load_mcp_server_configs(raw)
        assert cfg[0].risk_level is RiskLevel.HIGH

    def test_medium_is_preserved_and_is_not_low(self):
        cfg = load_mcp_server_configs(_servers(risk_level="medium"))
        assert cfg[0].risk_level is RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# Allowlist parsing
# ---------------------------------------------------------------------------


def _servers(**overrides):
    item = {
        "name": "catalog",
        "transport": "stdio",
        "command": "python3",
        "args": ["-m", "srv"],
        "allowed_tools": ["get_sku"],
        "risk_level": "low",
    }
    item.update(overrides)
    return json.dumps([item])


class TestLoadMcpServerConfigs:
    def test_valid_allowlist_is_parsed(self):
        cfg = load_mcp_server_configs(_servers())
        assert len(cfg) == 1
        assert cfg[0].name == "catalog"
        assert cfg[0].transport == "stdio"
        assert cfg[0].allowed_tools == ("get_sku",)

    def test_unparsable_json_yields_no_servers(self):
        # 整体不可解析 -> []（禁用 MCP），绝不"尽力而为"连接未声明的 server。
        assert load_mcp_server_configs("{not json") == []
        assert load_mcp_server_configs("") == []

    def test_non_array_json_yields_no_servers(self):
        assert load_mcp_server_configs('{"name": "catalog"}') == []

    def test_entry_without_name_is_skipped(self):
        raw = json.dumps([{"transport": "stdio", "command": "python3"}])
        assert load_mcp_server_configs(raw) == []

    def test_unknown_transport_is_rejected(self):
        raw = json.dumps(
            [
                {
                    "name": "catalog",
                    "transport": "carrier-pigeon",
                    "command": "python3",
                    "risk_level": "low",
                }
            ]
        )
        assert load_mcp_server_configs(raw) == []

    @pytest.mark.parametrize("transport", ["stdio", "sse"])
    def test_supported_transports_are_allowed(self, transport):
        raw = json.dumps(
            [
                {
                    "name": "catalog",
                    "transport": transport,
                    "command": "python3",
                    "url": "https://example.invalid/mcp",
                    "risk_level": "low",
                }
            ]
        )
        cfg = load_mcp_server_configs(raw)
        assert cfg[0].transport == transport

    def test_empty_allowed_tools_list_means_nothing_is_allowed(self):
        # 空 allowlist != 全部允许：这是 allowlist 语义的关键方向。
        cfg = load_mcp_server_configs(_servers(allowed_tools=[]))
        assert cfg[0].allowed_tools == ()

    def test_server_name_with_double_underscore_is_rejected(self):
        # `mcp__{server}__{tool}` 的分隔符就是 `__`，server 名里出现 `__`
        # 会让命名空间可被伪造。
        raw = json.dumps(
            [
                {
                    "name": "cat__alog",
                    "transport": "stdio",
                    "command": "python3",
                    "risk_level": "low",
                }
            ]
        )
        assert load_mcp_server_configs(raw) == []

    def test_defaults_are_applied_when_omitted(self):
        raw = json.dumps([{"name": "s", "transport": "stdio", "command": "x"}])
        cfg = load_mcp_server_configs(raw, default_timeout=7.5, default_max_payload=1234)
        assert cfg[0].timeout_seconds == 7.5
        assert cfg[0].max_payload_bytes == 1234

    def test_invalid_negative_timeout_fails_closed(self):
        raw = json.dumps(
            [{"name": "s", "transport": "stdio", "command": "x", "timeout_seconds": -1}]
        )
        assert load_mcp_server_configs(raw) == []


class TestBuildMcpAdapters:
    def test_disabled_server_is_skipped(self):
        cfg = load_mcp_server_configs(_servers(enabled=False))
        assert build_mcp_adapters(cfg) == []

    def test_enabled_server_yields_one_adapter(self):
        cfg = load_mcp_server_configs(_servers(enabled=True))
        assert len(build_mcp_adapters(cfg)) == 1


# ---------------------------------------------------------------------------
# Startup-time structural validation
# ---------------------------------------------------------------------------


class TestValidateMcpSettings:
    def test_disabled_needs_no_allowlist(self):
        assert validate_mcp_settings(enabled=False, servers="") == []

    def test_enabled_without_allowlist_is_rejected(self):
        errors = validate_mcp_settings(enabled=True, servers="")
        assert errors and "allowlist" in errors[0]

    def test_unparsable_json_is_rejected(self):
        assert validate_mcp_settings(enabled=True, servers="{nope")

    def test_non_array_is_rejected(self):
        assert validate_mcp_settings(enabled=True, servers='{"name":"a"}')

    def test_empty_array_is_rejected(self):
        assert validate_mcp_settings(enabled=True, servers="[]")

    def test_entry_without_name_is_rejected(self):
        assert validate_mcp_settings(enabled=True, servers='[{"transport":"stdio"}]')

    def test_missing_risk_level_is_reported_at_startup(self):
        errors = validate_mcp_settings(
            enabled=True, servers='[{"name":"a","transport":"stdio","command":"x"}]'
        )
        assert any("risk_level" in e for e in errors)

    def test_illegal_risk_level_is_reported_at_startup(self):
        errors = validate_mcp_settings(
            enabled=True,
            servers='[{"name":"a","transport":"stdio","command":"x","risk_level":"read"}]',
        )
        assert any("risk_level" in e for e in errors)

    def test_valid_allowlist_passes(self):
        assert (
            validate_mcp_settings(
                enabled=True,
                servers=(
                    '[{"name":"a","transport":"stdio","command":"x",'
                    '"allowed_tools":["t"],"risk_level":"low"}]'
                ),
            )
            == []
        )

    def test_non_positive_payload_cap_is_rejected(self):
        errors = validate_mcp_settings(enabled=True, servers=_servers(), max_payload_bytes=0)
        assert any("MCP_MAX_PAYLOAD_BYTES" in e for e in errors)


# ---------------------------------------------------------------------------
# ToolRegistry source is observability-only
# ---------------------------------------------------------------------------


class TestRegistrySourceIsObservabilityOnly:
    def test_default_source_is_native(self):
        reg = ToolRegistry()
        reg.register(
            name="t",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: {"ok": True},
        )
        assert reg.source_for("t") == SOURCE_NATIVE
        assert reg.tools_by_source(SOURCE_MCP) == []

    def test_explicit_mcp_source_is_recorded(self):
        reg = ToolRegistry()
        reg.register(
            name="mcp__catalog__get_sku",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: {"ok": True},
            risk_level="low",
            source=SOURCE_MCP,
        )
        assert reg.source_for("mcp__catalog__get_sku") == SOURCE_MCP
        assert reg.tools_by_source(SOURCE_MCP) == ["mcp__catalog__get_sku"]

    def test_unknown_tool_has_no_source(self):
        assert ToolRegistry().source_for("absent") is None

    def test_unknown_source_yields_empty_list(self):
        assert ToolRegistry().tools_by_source("carrier-pigeon") == []

    async def test_source_does_not_change_execution_semantics(self):
        # `source` 纯粹是可观测性维度：同样的 handler + risk_level，
        # native 与 mcp 来源的执行结果必须一致（不因来源而改写行为）。
        results = []
        for source in (SOURCE_NATIVE, SOURCE_MCP):
            reg = ToolRegistry()
            reg.register(
                name="t",
                description="d",
                parameters={"type": "object", "properties": {}},
                handler=lambda args: {"echo": args["v"]},
                risk_level="low",
                source=source,
            )
            results.append(await reg.execute("t", {"v": 7}))
        assert results[0] == results[1]


# ---------------------------------------------------------------------------
# Error taxonomy is reachable and distinct
# ---------------------------------------------------------------------------


class TestErrorTaxonomy:
    def test_configuration_error_is_exported(self):
        assert issubclass(MCPConfigurationError, Exception)

    def test_name_prefix_constant_is_the_documented_one(self):
        assert MCP_NAME_PREFIX == "mcp__"
