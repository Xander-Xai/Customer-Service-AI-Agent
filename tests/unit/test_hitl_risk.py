"""HITL 风险分级测试。

治理前提：只有 HIGH 需要人工审批。LOW / MEDIUM 若被拦，就是治理误伤——它会把
普通问答拖进审批流程，是本能力最常见的失败方式。
"""

from __future__ import annotations

import pytest

from core.hitl.risk import RiskLevel, classify_risk, requires_approval


class TestRequiresApproval:
    def test_only_high_requires_approval(self):
        assert requires_approval(RiskLevel.HIGH) is True
        assert requires_approval(RiskLevel.MEDIUM) is False
        assert requires_approval(RiskLevel.LOW) is False

    @pytest.mark.parametrize("value", ["high", "HIGH", "high "])
    def test_accepts_string_forms(self, value):
        assert requires_approval(value) is True

    @pytest.mark.parametrize("value", ["", "critical", None, 3])
    def test_unknown_is_not_approved(self, value):
        """未知等级 fail-closed 于「不审批」这一侧之外：这里断言它不会被当 HIGH。

        注意语义：requires_approval 只回答「是否 HIGH」。真正的 fail-closed 在
        调用方——未知等级应被显式处理，而不是静默升级为 HIGH。
        """
        assert requires_approval(value) is False


class TestClassifyRisk:
    def test_explicit_declaration_wins(self, monkeypatch):
        """工具定义处的显式声明优先级最高（高于白名单与金额）。"""
        monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "some_tool")
        assert classify_risk("some_tool", explicit="low") is RiskLevel.LOW

    def test_explicit_invalid_falls_through_to_inference(self, monkeypatch):
        monkeypatch.setattr("core.config.HITL_MEDIUM_RISK_TOOLS", "t")
        assert classify_risk("t", explicit="nonsense") is RiskLevel.MEDIUM

    def test_name_allowlist_high(self, monkeypatch):
        monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "staging_refund, erp_refund")
        monkeypatch.setattr("core.config.HITL_MEDIUM_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_HIGH_AMOUNT_THRESHOLD", 0.0)
        assert classify_risk("staging_refund") is RiskLevel.HIGH
        assert classify_risk("ERP_REFUND") is RiskLevel.HIGH  # 大小写不敏感
        assert classify_risk("erp_refund") is RiskLevel.HIGH

    def test_name_allowlist_medium(self, monkeypatch):
        monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_MEDIUM_RISK_TOOLS", "create_ticket")
        monkeypatch.setattr("core.config.HITL_HIGH_AMOUNT_THRESHOLD", 0.0)
        assert classify_risk("create_ticket") is RiskLevel.MEDIUM

    def test_amount_threshold_promotes_to_high(self, monkeypatch):
        """金额维度覆盖白名单之外的大额写操作。"""
        monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_MEDIUM_RISK_TOOLS", "adjust")
        monkeypatch.setattr("core.config.HITL_HIGH_AMOUNT_THRESHOLD", 1000.0)
        assert classify_risk("adjust", arguments={"amount": 999}) is RiskLevel.MEDIUM
        assert classify_risk("adjust", arguments={"amount": 1000}) is RiskLevel.HIGH
        assert classify_risk("adjust", arguments={"refund_amount": 5000}) is RiskLevel.HIGH

    def test_amount_threshold_disabled_when_zero(self, monkeypatch):
        monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_MEDIUM_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_HIGH_AMOUNT_THRESHOLD", 0.0)
        assert classify_risk("whatever", arguments={"amount": 10**9}) is RiskLevel.LOW

    def test_non_numeric_amount_is_ignored_not_raised(self, monkeypatch):
        """脏参数不能让分级崩掉，更不能变成 HIGH。"""
        monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_MEDIUM_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_HIGH_AMOUNT_THRESHOLD", 100.0)
        assert classify_risk("t", arguments={"amount": "abc"}) is RiskLevel.LOW
        assert classify_risk("t", arguments={"amount": None}) is RiskLevel.LOW
        assert classify_risk("t", arguments=None) is RiskLevel.LOW

    def test_unknown_tool_defaults_low(self, monkeypatch):
        monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_MEDIUM_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_HIGH_AMOUNT_THRESHOLD", 0.0)
        assert classify_risk("never_registered_tool") is RiskLevel.LOW

    def test_empty_tool_name_is_low(self, monkeypatch):
        monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_MEDIUM_RISK_TOOLS", "")
        monkeypatch.setattr("core.config.HITL_HIGH_AMOUNT_THRESHOLD", 0.0)
        assert classify_risk("") is RiskLevel.LOW

    def test_only_high_reaches_requires_approval_end_to_end(self, monkeypatch):
        """端到端：LOW/MEDIUM 不产生审批，HIGH 产生。"""
        monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "refund")
        monkeypatch.setattr("core.config.HITL_MEDIUM_RISK_TOOLS", "ticket")
        monkeypatch.setattr("core.config.HITL_HIGH_AMOUNT_THRESHOLD", 0.0)
        assert requires_approval(classify_risk("refund")) is True
        assert requires_approval(classify_risk("ticket")) is False
        assert requires_approval(classify_risk("chat")) is False
