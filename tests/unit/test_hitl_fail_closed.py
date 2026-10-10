"""HITL fail-closed 治理契约（配置写漏 = 审批形同虚设的修复与回归锁定）。

修复前的问题
------------
``HITL_ENABLED=true`` 时，若 ``HITL_HIGH_RISK_TOOLS`` 为空**且**
``HITL_HIGH_AMOUNT_THRESHOLD<=0``，``core.hitl.risk.classify_risk`` 的判定链
一路落到默认值 ``LOW`` -> ``requires_approval`` 恒为 ``False`` -> 审批闸门
**什么都不拦**，且没有任何日志、指标或告警。

它危险的地方在于「声称与事实不符」：配置写着审批已启用，运行时却没有审批；
而且发生在最常见的事故形态下（开了开关、忘了配规则）。

两层互补的防线
--------------
1. **启动校验**（``core.config.validate_hitl_settings``）：配置根本不可能产生任何
   HIGH 判定 -> 拒绝启动。
2. **执行期兜底**（``classify_risk(side_effect=...)``）：个别有副作用的工具没有被
   任何规则覆盖 -> 按 HIGH 处理。

它们**不能互相替代**：只做 1，第 2 个覆盖缺口依然存在；只做 2，配置写漏时整个
治理边界仍是空的。

本文件同时锁定「不得通过默认允许来提升测试通过率」：所有治理收紧都发生在
**有副作用**的工具上，只读查询与免审批任务的执行路径必须原样保留。
"""

from __future__ import annotations

import pytest

from core.config import validate_hitl_settings
from core.hitl.risk import RiskLevel, classify_risk, requires_approval


@pytest.fixture(autouse=True)
def _hitl_enabled(monkeypatch):
    """本文件测的是「治理**已开启**」时的行为；默认保持开启。"""
    monkeypatch.setattr("core.config.HITL_ENABLED", True)
    monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "staging_refund")
    monkeypatch.setattr("core.config.HITL_MEDIUM_RISK_TOOLS", "")
    monkeypatch.setattr("core.config.HITL_HIGH_AMOUNT_THRESHOLD", 0.0)


# ─────────────────── 第 1 层：启动校验（fail closed） ───────────────────


class TestStartupValidation:
    def test_disabled_governance_is_always_valid(self):
        """默认关闭时不校验 —— 治理边界本就不在承诺范围内，不该阻止启动。"""
        assert (
            validate_hitl_settings(
                enabled=False, high_risk_tools="", medium_risk_tools="", high_amount_threshold=0.0
            )
            == []
        )

    def test_empty_rules_with_governance_on_is_rejected(self):
        """历史缺陷本体：开了开关却没有任何规则能命中 HIGH。"""
        errors = validate_hitl_settings(
            enabled=True, high_risk_tools="", medium_risk_tools="", high_amount_threshold=0.0
        )
        assert errors, "HITL_ENABLED=true + 无规则 竟然通过校验（fail-open）"
        joined = " ".join(errors)
        assert "HITL_HIGH_RISK_TOOLS" in joined, joined
        assert "HITL_HIGH_AMOUNT_THRESHOLD" in joined, joined

    def test_high_risk_name_list_alone_is_enough(self):
        assert (
            validate_hitl_settings(
                enabled=True,
                high_risk_tools="refund,order_change",
                medium_risk_tools="",
                high_amount_threshold=0.0,
            )
            == []
        )

    def test_amount_threshold_alone_is_enough(self):
        assert (
            validate_hitl_settings(
                enabled=True, high_risk_tools="", medium_risk_tools="", high_amount_threshold=100.0
            )
            == []
        )

    def test_zero_threshold_with_names_is_fine_but_threshold_only_zero_is_not(self):
        """阈值 0 = 关闭金额维度，本身不是错；但它也不能单独构成治理。"""
        assert (
            validate_hitl_settings(
                enabled=True,
                high_risk_tools="refund",
                medium_risk_tools="",
                high_amount_threshold=0.0,
            )
            == []
        )
        assert validate_hitl_settings(
            enabled=True, high_risk_tools="  ,  ", medium_risk_tools="", high_amount_threshold=0.0
        ), "仅由逗号/空白组成的白名单不应被当作有效规则"

    def test_non_positive_ttl_is_rejected(self):
        """TTL<=0 会让审批「立即过期并按拒绝处理」= 治理退化成「一律拒绝」。"""
        assert validate_hitl_settings(
            enabled=True, high_risk_tools="refund", ttl_seconds=0.0
        ), "TTL=0 未被拒绝（治理会退化成永远拒绝）"
        assert validate_hitl_settings(
            enabled=True, high_risk_tools="refund", ttl_seconds=-5.0
        ), "TTL<0 未被拒绝"

    def test_contradictory_allowlists_are_rejected(self):
        errors = validate_hitl_settings(
            enabled=True, high_risk_tools="refund", medium_risk_tools="refund"
        )
        assert errors, "HIGH 与 MEDIUM 白名单完全相同却通过了校验"

    def test_validate_required_config_raises_on_invalid_hitl(self):
        """端到端：配置写漏时**启动失败**，而不是带着假治理跑起来。"""
        from core import config as config_module

        # 注意：异常类必须**经模块对象**取，不能用文件顶部 `from core.config import
        # ConfigurationError` 的绑定。同目录的 tests/unit/test_hitl_api.py 会
        # `importlib.reload(core.config)`，reload 会重建类对象，届时顶层绑定的
        # 就是另一个类，`pytest.raises` 反而抓不到自己触发的异常 —— 一个只在全量
        # 运行时出现的假失败。
        original = (
            config_module.HITL_ENABLED,
            config_module.HITL_HIGH_RISK_TOOLS,
            config_module.HITL_MEDIUM_RISK_TOOLS,
            config_module.HITL_HIGH_AMOUNT_THRESHOLD,
        )
        config_module.HITL_ENABLED = True
        config_module.HITL_HIGH_RISK_TOOLS = ""
        config_module.HITL_MEDIUM_RISK_TOOLS = ""
        config_module.HITL_HIGH_AMOUNT_THRESHOLD = 0.0
        try:
            with pytest.raises(config_module.ConfigurationError) as exc:
                config_module.validate_required_config()
            assert "HITL" in str(exc.value)
        finally:
            (
                config_module.HITL_ENABLED,
                config_module.HITL_HIGH_RISK_TOOLS,
                config_module.HITL_MEDIUM_RISK_TOOLS,
                config_module.HITL_HIGH_AMOUNT_THRESHOLD,
            ) = original


# ─────────────────── 第 2 层：执行期 side-effect 兜底 ───────────────────


class TestUncoveredSideEffectDefaultsToHigh:
    def test_side_effect_without_any_rule_is_high(self):
        """覆盖缺口：有副作用、没声明 risk_level、不在任何白名单 -> HIGH。"""
        assert classify_risk("mystery_write_tool", side_effect=True) is RiskLevel.HIGH
        assert requires_approval(classify_risk("mystery_write_tool", side_effect=True)) is True

    def test_read_only_tool_is_unaffected(self):
        """普通只读查询必须保持 LOW —— 治理收紧不得波及正常问答路径。"""
        assert classify_risk("query_order", side_effect=False) is RiskLevel.LOW
        assert requires_approval(classify_risk("query_order", side_effect=False)) is False

    def test_forgetting_to_pass_side_effect_does_not_grant_safety(self):
        """``side_effect`` 默认 False，调用方必须显式声明。

        反过来看这条的意图：默认 ``False`` 意味着「忘了传」不会自动把工具变成
        高危（否则每个未知调用都会被拦，治理就变成噪音）。真正的防线是
        :meth:`ToolRegistry.is_side_effect` —— 它以**注册表声明**为准，而不是
        以调用方的记忆为准（见 test_ungoverned_and_covered_use_the_registry_fact）。
        """
        assert classify_risk("mystery_write_tool") is RiskLevel.LOW

    def test_explicit_declaration_still_wins_over_the_default(self):
        """运维显式声明 low 的只读/低风险工具不被兜底抬高。"""
        assert classify_risk("create_ticket", explicit="low", side_effect=True) is RiskLevel.LOW

    def test_medium_allowlist_still_wins_over_the_default(self):
        """有意放行有专门的表达方式：写进 MEDIUM 白名单。"""
        import core.config as config_module

        original = config_module.HITL_MEDIUM_RISK_TOOLS
        config_module.HITL_MEDIUM_RISK_TOOLS = "create_ticket"
        try:
            assert classify_risk("create_ticket", side_effect=True) is RiskLevel.MEDIUM
            assert requires_approval(classify_risk("create_ticket", side_effect=True)) is False
        finally:
            config_module.HITL_MEDIUM_RISK_TOOLS = original

    def test_amount_threshold_still_wins(self):
        import core.config as config_module

        original = config_module.HITL_HIGH_AMOUNT_THRESHOLD
        config_module.HITL_HIGH_AMOUNT_THRESHOLD = 100.0
        try:
            assert classify_risk("adjust", arguments={"amount": 500}, side_effect=False) is (
                RiskLevel.HIGH
            )
        finally:
            config_module.HITL_HIGH_AMOUNT_THRESHOLD = original

    def test_disabled_governance_does_not_escalate(self):
        """治理关闭时不兜底 —— 否则关掉 HITL 反而会拦所有写工具。"""
        import core.config as config_module

        original = config_module.HITL_ENABLED
        config_module.HITL_ENABLED = False
        try:
            assert classify_risk("mystery_write_tool", side_effect=True) is RiskLevel.LOW
        finally:
            config_module.HITL_ENABLED = original


class TestGatePassesRegistryFacts:
    def test_ungoverned_and_covered_use_the_registry_fact(self):
        """闸门必须用**注册表**的 side_effect 声明，而不是调用方的默认值。"""
        from core.hitl.gate import should_propose_approval
        from tools.tool_registry import ToolRegistry

        registry = ToolRegistry()

        async def _noop(arguments):
            return "ok"

        registry.register(
            name="undeclared_write",
            description="写工具但没声明 risk_level",
            parameters={"type": "object", "properties": {}},
            handler=_noop,
            side_effect=True,
        )
        registry.register(
            name="plain_read",
            description="只读工具",
            parameters={"type": "object", "properties": {}},
            handler=_noop,
            side_effect=False,
        )

        import core.config as config_module
        from runtime.context import reset_run_context, set_run_context

        original_enabled = config_module.HITL_ENABLED
        config_module.HITL_ENABLED = True
        tokens = set_run_context("run-hitl-failclosed-1", "thread-1")
        try:
            assert (
                should_propose_approval("undeclared_write", {}, registry) is True
            ), "有副作用但未声明风险的工具未被闸门拦下 —— 覆盖缺口仍然 fail-open"
            assert (
                should_propose_approval("plain_read", {}, registry) is False
            ), "只读工具被误拦 —— 治理误伤会把普通问答拖进审批"
        finally:
            reset_run_context(tokens)
            config_module.HITL_ENABLED = original_enabled

    def test_fast_path_without_run_context_is_not_approval_gated(self):
        """无 run 上下文时闸门不拦（拦了也无法挂起/恢复）。

        该路径的安全边界是 ``ToolRegistry`` 的 fail-closed **拒绝执行**，
        而不是审批 —— 两条机制各司其职，不得互相冒充。
        """
        from core.hitl.gate import is_ungoverned_side_effect, should_propose_approval
        from tools.tool_registry import ToolRegistry

        registry = ToolRegistry()

        async def _noop(arguments):
            return "ok"

        registry.register(
            name="undeclared_write",
            description="写工具",
            parameters={"type": "object", "properties": {}},
            handler=_noop,
            side_effect=True,
        )
        import core.config as config_module

        original_enabled = config_module.HITL_ENABLED
        config_module.HITL_ENABLED = True
        try:
            assert should_propose_approval("undeclared_write", {}, registry) is False
            assert is_ungoverned_side_effect("undeclared_write", {}, registry) is True
        finally:
            config_module.HITL_ENABLED = original_enabled
