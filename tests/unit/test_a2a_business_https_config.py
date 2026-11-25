"""Operator-only HTTPS mode does not weaken the synthetic loopback contract."""

import pytest

from integrations.a2a_policy.business_client import register_business_a2a_tool
from integrations.a2a_policy.business_contract import require_business_config
from integrations.a2a_policy.contracts import (
    A2AContractError,
    A2AIntegrationDisabled,
    validate_loopback_base_url,
    validate_private_https_base_url,
)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://policy.internal.example",
        "https://127.0.0.1",
        "https://policy.internal.example@evil.example",
        "https://policy.internal.example/redirect",
        "https://policy.internal.example?x=1",
        "https://policy.internal.example:0",
        "https://policy.internal.example:99999",
    ],
)
def test_private_peer_endpoint_rejects_unsafe_origins(endpoint):
    with pytest.raises(A2AContractError):
        validate_private_https_base_url(endpoint)


def test_private_peer_cannot_enable_synthetic_remote_transport():
    endpoint = "https://policy.internal.example:8443"
    assert validate_private_https_base_url(endpoint) == endpoint
    with pytest.raises(A2AContractError):
        validate_loopback_base_url(endpoint)


def test_production_needs_explicit_https_and_ca(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("A2A_AFTERSALES_ENABLED", "true")
    monkeypatch.setenv("A2A_AFTERSALES_TOKEN", "b" * 64)
    monkeypatch.setenv("A2A_AFTERSALES_POLICY_SHA256", "a" * 64)
    monkeypatch.setenv("A2A_AFTERSALES_ENDPOINT", "https://policy.internal.example")
    monkeypatch.delenv("A2A_AFTERSALES_PRIVATE_HTTPS", raising=False)
    monkeypatch.delenv("A2A_AFTERSALES_CA_FILE", raising=False)
    with pytest.raises(A2AIntegrationDisabled):
        require_business_config()
    monkeypatch.setenv("A2A_AFTERSALES_PRIVATE_HTTPS", "true")
    with pytest.raises(A2AIntegrationDisabled):
        require_business_config()
    ca = tmp_path / "ca.pem"
    ca.touch()
    monkeypatch.setenv("A2A_AFTERSALES_CA_FILE", str(ca))
    with pytest.raises(A2AIntegrationDisabled):
        require_business_config()
    ca.write_text("fixture path only; transport verifies actual certificate", encoding="utf-8")
    monkeypatch.setenv("A2A_AFTERSALES_CA_FILE", str(ca))
    assert require_business_config()[0] == "https://policy.internal.example"
    monkeypatch.setenv("A2A_AFTERSALES_ENDPOINT", "http://127.0.0.1:41243")
    with pytest.raises(A2AContractError):
        require_business_config()


def test_internal_http_peer_is_limited_to_exact_local_compose_service(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("A2A_AFTERSALES_ENABLED", "true")
    monkeypatch.setenv("A2A_AFTERSALES_INTERNAL_HTTP", "true")
    monkeypatch.setenv("A2A_AFTERSALES_ENDPOINT", "http://a2a-peer:41243")
    monkeypatch.setenv("A2A_AFTERSALES_TOKEN", "b" * 64)
    monkeypatch.setenv("A2A_AFTERSALES_POLICY_SHA256", "a" * 64)
    assert require_business_config()[0] == "http://a2a-peer:41243"

    monkeypatch.setenv("A2A_AFTERSALES_ENDPOINT", "http://example.com:41243")
    with pytest.raises(A2AIntegrationDisabled, match="isolated a2a-peer"):
        require_business_config()


def test_explicit_a2a_rollback_does_not_register_business_tool(monkeypatch):
    monkeypatch.setenv("A2A_AFTERSALES_ENABLED", "false")

    class Registry:
        def list_tools(self):
            return []

    assert register_business_a2a_tool(Registry()) is False


def test_internal_http_peer_rejected_in_industrial_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("A2A_AFTERSALES_ENABLED", "true")
    monkeypatch.setenv("A2A_AFTERSALES_INTERNAL_HTTP", "true")
    monkeypatch.setenv("A2A_AFTERSALES_ENDPOINT", "http://a2a-peer:41243")
    monkeypatch.setenv("A2A_AFTERSALES_TOKEN", "b" * 64)
    monkeypatch.setenv("A2A_AFTERSALES_POLICY_SHA256", "a" * 64)
    with pytest.raises(A2AIntegrationDisabled, match="non-industrial"):
        require_business_config()


def test_industrial_profile_requires_private_https_even_without_prod_app_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "industrial")
    monkeypatch.setenv("A2A_AFTERSALES_ENABLED", "true")
    monkeypatch.setenv("A2A_AFTERSALES_PRIVATE_HTTPS", "false")
    monkeypatch.setenv("A2A_AFTERSALES_INTERNAL_HTTP", "false")
    monkeypatch.setenv("A2A_AFTERSALES_ENDPOINT", "http://127.0.0.1:41243")
    monkeypatch.setenv("A2A_AFTERSALES_TOKEN", "b" * 64)
    monkeypatch.setenv("A2A_AFTERSALES_POLICY_SHA256", "a" * 64)
    with pytest.raises(A2AIntegrationDisabled, match="requires private HTTPS"):
        require_business_config()
