"""HITL 风险策略测试。"""

import pytest

from core.hitl.risk import RiskLevel, classify_risk, requires_approval


@pytest.mark.unit
def test_high_risk_tools_require_approval(monkeypatch):
    import core.config as cfg

    monkeypatch.setattr(cfg, "HITL_HIGH_RISK_TOOLS", "refund,modify_order")
    monkeypatch.setattr(cfg, "HITL_MEDIUM_RISK_TOOLS", "create_after_sales_ticket")
    monkeypatch.setattr(cfg, "HITL_HIGH_AMOUNT_THRESHOLD", 1000.0)

    assert classify_risk("refund") is RiskLevel.HIGH
    assert classify_risk("modify_order") is RiskLevel.HIGH
    assert classify_risk("create_after_sales_ticket") is RiskLevel.MEDIUM
    assert classify_risk("query_order") is RiskLevel.LOW
    assert requires_approval(RiskLevel.HIGH) is True
    assert requires_approval(RiskLevel.MEDIUM) is False
    assert requires_approval(RiskLevel.LOW) is False


@pytest.mark.unit
def test_high_amount_escalates_to_high(monkeypatch):
    import core.config as cfg

    monkeypatch.setattr(cfg, "HITL_HIGH_RISK_TOOLS", "refund")
    monkeypatch.setattr(cfg, "HITL_MEDIUM_RISK_TOOLS", "")
    monkeypatch.setattr(cfg, "HITL_HIGH_AMOUNT_THRESHOLD", 1000.0)

    # 工具名不在 high 列表，但金额超过阈值 -> HIGH
    assert classify_risk("compensate", arguments={"amount": 5000}) is RiskLevel.HIGH
    assert classify_risk("compensate", arguments={"amount": 10}) is RiskLevel.LOW
    # 无法解析的金额不误判
    assert classify_risk("compensate", arguments={"amount": "unknown"}) is RiskLevel.LOW


@pytest.mark.unit
def test_explicit_risk_level_wins(monkeypatch):
    import core.config as cfg

    monkeypatch.setattr(cfg, "HITL_HIGH_RISK_TOOLS", "")
    monkeypatch.setattr(cfg, "HITL_MEDIUM_RISK_TOOLS", "")
    monkeypatch.setattr(cfg, "HITL_HIGH_AMOUNT_THRESHOLD", 0.0)

    assert classify_risk("whatever", explicit="high") is RiskLevel.HIGH
    assert classify_risk("refund", explicit="low") is RiskLevel.LOW
