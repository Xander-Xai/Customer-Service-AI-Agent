"""No-SDK checks for feature gating, data minimization and SSRF prevention."""

import pytest

from integrations.a2a_policy.contracts import (
    A2AContractError,
    A2AIntegrationDisabled,
    require_demo_enabled,
    require_token,
    synthetic_policy_answer,
    validate_loopback_base_url,
    validate_topic,
)
from tools.a2a_policy_client import register_synthetic_a2a_tool


class RegistryStub:
    def __init__(self):
        self.registered = {}

    def list_tools(self):
        return list(self.registered)

    def register(self, **kwargs):
        self.registered[kwargs["name"]] = kwargs


def test_a2a_disabled_default(monkeypatch):
    monkeypatch.delenv("A2A_FAQ_EXPERIMENT_ENABLED", raising=False)
    assert register_synthetic_a2a_tool(RegistryStub()) is False
    with pytest.raises(A2AIntegrationDisabled):
        require_demo_enabled()


def test_production_and_unset_environment_fail_closed(monkeypatch):
    monkeypatch.setenv("A2A_FAQ_EXPERIMENT_ENABLED", "true")
    for env in ("production", "staging", ""):
        monkeypatch.setenv("APP_ENV", env)
        with pytest.raises(A2AIntegrationDisabled):
            require_demo_enabled()


def test_weak_token_is_rejected(monkeypatch):
    monkeypatch.setenv("A2A_FAQ_SHARED_TOKEN", "short")
    with pytest.raises(A2AIntegrationDisabled):
        require_token()


@pytest.mark.parametrize(
    "topic",
    ["", "refund", "真实退款订单", "returns_demo; rm -rf /", " returns_demo", None, 123],
)
def test_only_synthetic_topic_enum_is_allowed(topic):
    with pytest.raises(A2AContractError):
        validate_topic(topic)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://169.254.169.254:80",
        "http://127.0.0.1:9001/other",
        "http://user:pass@127.0.0.1:9001",
        "http://127.0.0.1:9001#fragment",
        "http://127.0.0.1:9001?next=foo",
        "file:///etc/passwd",
        "http://localhost",
    ],
)
def test_no_arbitrary_remote_endpoint_or_redirects(url):
    with pytest.raises(A2AContractError):
        validate_loopback_base_url(url)


def test_valid_readonly_service_tool_registration(monkeypatch):
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("A2A_FAQ_EXPERIMENT_ENABLED", "true")
    monkeypatch.setenv("A2A_FAQ_SHARED_TOKEN", "a" * 64)
    monkeypatch.setenv("A2A_FAQ_ENDPOINT", "http://127.0.0.1:41243")
    registry = RegistryStub()
    assert register_synthetic_a2a_tool(registry)
    tool = registry.registered["synthetic_a2a_faq_lookup"]
    assert tool["source"] == "a2a"
    assert tool["side_effect"] is False
    assert tool["cache_policy"].enabled is False
    assert tool["parameters"]["additionalProperties"] is False
    with pytest.raises(A2AContractError):
        register_synthetic_a2a_tool(registry)


def test_only_fictional_policy_data_can_be_resolved():
    for topic in ("returns_demo", "invoice_demo", "shipping_demo"):
        response = synthetic_policy_answer(topic)
        assert response.startswith("SYNTHETIC EXAMPLE ONLY:")
        assert "real" in response.lower() or "authorized" in response.lower()
