"""ERP mode admission must not turn a real deployment into synthetic data."""

from unittest.mock import Mock

import pytest

from erp import factory
from erp.kingdee_adapter import KingdeeMockAdapter
from erp.kingdee_real_adapter import KingdeeRealAdapter


def set_real_config(monkeypatch):
    monkeypatch.setattr(factory, "ERP_MODE", "real")
    monkeypatch.setattr(factory, "ERP_BASE_URL", "https://erp.example.invalid")
    monkeypatch.setattr(factory, "ERP_APP_ID", "test-app")
    monkeypatch.setattr(factory, "ERP_APP_SECRET", "test-secret")
    monkeypatch.setattr(factory, "ERP_DB_ID", "test-db")


def test_explicit_mock_mode_is_supported(monkeypatch):
    monkeypatch.setattr(factory, "ERP_MODE", "mock")
    assert isinstance(factory.create_erp_adapter(), KingdeeMockAdapter)


def test_unknown_mode_fails_closed(monkeypatch):
    monkeypatch.setattr(factory, "ERP_MODE", "typo-private")
    with pytest.raises(factory.ERPConfigurationError, match="Unsupported ERP_MODE") as error:
        factory.create_erp_adapter()
    assert "typo-private" not in str(error.value)


@pytest.mark.parametrize("field", ["ERP_BASE_URL", "ERP_APP_ID", "ERP_APP_SECRET", "ERP_DB_ID"])
def test_missing_real_configuration_never_selects_mock(monkeypatch, field):
    set_real_config(monkeypatch)
    monkeypatch.setattr(factory, field, " ")
    with pytest.raises(factory.ERPConfigurationError, match=field):
        factory.create_erp_adapter()


def test_real_adapter_constructed_without_network(monkeypatch):
    set_real_config(monkeypatch)
    assert isinstance(factory.create_erp_adapter(), KingdeeRealAdapter)


def test_real_constructor_failure_is_redacted(monkeypatch):
    set_real_config(monkeypatch)
    monkeypatch.setattr(
        "erp.kingdee_real_adapter.KingdeeRealAdapter",
        Mock(side_effect=RuntimeError("private-credential-detail")),
    )
    with pytest.raises(factory.ERPConfigurationError, match="initialization failed") as error:
        factory.create_erp_adapter()
    assert "private-credential-detail" not in str(error.value)
    assert error.value.__suppress_context__ is True
