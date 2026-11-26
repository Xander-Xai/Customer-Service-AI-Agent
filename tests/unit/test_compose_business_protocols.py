"""Standard deployment keeps MCP/A2A on the real business path and ordered."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/compose/docker-compose.yml"
CANARY = ROOT / "deploy/compose/docker-compose.canary.yml"
ENV_EXAMPLE = ROOT / ".env.example"


def test_standard_compose_wires_mcp_and_a2a_before_api_and_worker():
    config = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = config["services"]
    app = services["app"]
    worker = services["worker"]
    peer = services["a2a-peer"]
    init = services["protocol-init"]

    for service in (app, worker):
        dependencies = service["depends_on"]
        assert dependencies["protocol-init"]["condition"] == "service_completed_successfully"
        assert dependencies["a2a-peer"]["condition"] == "service_healthy"
        assert "/app/scripts/exec_with_protocol_config.sh" in service["entrypoint"]
        assert "protocol-config:/run/csai-protocol:ro" in service["volumes"]
        assert "${A2A_AFTERSALES_CA_SOURCE:-/dev/null}:/run/a2a-ca/ca.pem:ro" in service["volumes"]
        assert (
            "${MCP_PRODUCT_CATALOG_SOURCE:-/dev/null}:/run/mcp/product-docs.json:ro"
            in service["volumes"]
        )

    assert "ports" not in peer
    assert peer["networks"] == ["protocol-network"]
    assert "A2A_AFTERSALES_ENABLED=${A2A_AFTERSALES_ENABLED:-true}" in peer["environment"]
    assert "A2A_AFTERSALES_ENABLED" in " ".join(peer["command"])
    assert peer["depends_on"]["protocol-init"]["condition"] == "service_completed_successfully"
    assert any("41243" in str(item) for item in peer["healthcheck"]["test"])
    assert init["user"] == "0:0"
    assert "protocol-config:/run/csai-protocol" in init["volumes"]
    assert config["networks"]["protocol-network"]["internal"] is True
    assert "protocol-network" in app["networks"]
    assert "protocol-network" in worker["networks"]


def test_standard_compose_default_has_explicit_rollback_and_fail_closed():
    text = COMPOSE.read_text(encoding="utf-8")
    assert "MCP_ENABLED=${MCP_ENABLED:-true}" in text
    assert "MCP_FAIL_CLOSED=${MCP_FAIL_CLOSED:-true}" in text
    assert "A2A_AFTERSALES_ENABLED=${A2A_AFTERSALES_ENABLED:-true}" in text
    assert "CSAI_PROTOCOL_MODE=${CSAI_PROTOCOL_MODE:-synthetic}" in text


def test_worker_healthcheck_loads_generated_protocol_configuration():
    worker = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["worker"]
    healthcheck = " ".join(worker["healthcheck"]["test"])

    assert "/app/scripts/exec_with_protocol_config.sh" in healthcheck
    assert "celery -A runtime.celery_app:celery_app inspect ping" in healthcheck


def test_non_compose_runtime_example_does_not_enable_empty_protocol_config():
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "MCP_ENABLED=\n" in env
    assert "A2A_AFTERSALES_ENABLED=\n" in env


def test_canary_overlay_uses_the_same_protocol_readiness_boundary():
    config = yaml.safe_load(CANARY.read_text(encoding="utf-8"))
    canary = config["services"]["canary"]
    dependencies = canary["depends_on"]
    assert dependencies["protocol-init"]["condition"] == "service_completed_successfully"
    assert dependencies["a2a-peer"]["condition"] == "service_healthy"
    assert "/app/scripts/exec_with_protocol_config.sh" in canary["entrypoint"]
    assert "protocol-config:/run/csai-protocol:ro" in canary["volumes"]
    assert "${A2A_AFTERSALES_CA_SOURCE:-/dev/null}:/run/a2a-ca/ca.pem:ro" in canary["volumes"]
    assert (
        "${MCP_PRODUCT_CATALOG_SOURCE:-/dev/null}:/run/mcp/product-docs.json:ro"
        in canary["volumes"]
    )
    assert "protocol-network" in canary["networks"]
