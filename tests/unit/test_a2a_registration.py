"""Registry bridge tests run in the main app's dependency-rich CI lane."""

import pytest

from integrations.a2a_policy.contracts import A2AContractError
from tools.a2a_policy_client import register_synthetic_a2a_tool


class RegistryStub:
    def __init__(self):
        self.registered = {}

    def list_tools(self):
        return list(self.registered)

    def register(self, **kwargs):
        self.registered[kwargs["name"]] = kwargs


def test_disabled_default_registers_no_tools(monkeypatch):
    monkeypatch.delenv("A2A_FAQ_EXPERIMENT_ENABLED", raising=False)
    assert register_synthetic_a2a_tool(RegistryStub()) is False


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
