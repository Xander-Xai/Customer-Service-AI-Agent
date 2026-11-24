"""Real official A2A SDK 1.x interop tests using isolated HTTP ASGI transport.

The dedicated .github/workflows/a2a-interop.yml MUST install the SDK and run
these tests. The default repository suite may skip when optional SDK is absent.
"""

import asyncio
import os
import socket
import subprocess
import sys

import httpx
import pytest

from integrations.a2a_policy.client import lookup_synthetic_faq
from integrations.a2a_policy.contracts import A2AContractError

BASE = "http://127.0.0.1:41243"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("A2A_FAQ_EXPERIMENT_ENABLED", "true")
    monkeypatch.setenv("A2A_FAQ_SHARED_TOKEN", "b" * 64)
    monkeypatch.setenv("A2A_FAQ_ENDPOINT", BASE)
    pytest.importorskip("a2a", reason="optional A2A 1.x SDK not installed")
    from integrations.a2a_policy.server import build_app

    return build_app(BASE)


@pytest.mark.asyncio
async def test_card_discovery_and_bearer_protection(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as peer:
        card_response = await peer.get("/.well-known/agent-card.json")
        assert card_response.status_code == 200
        card = card_response.json()
        assert card["name"] == "Synthetic Customer FAQ Specialist"
        assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
        assert card["securityRequirements"]
        result = await peer.post("/a2a/jsonrpc", json={})
        assert result.status_code == 401
        assert "Bearer" in result.headers["WWW-Authenticate"]


@pytest.mark.asyncio
async def test_real_a2a_client_to_server_roundtrip(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE,
        headers={"Authorization": "Bearer " + "b" * 64},
    ) as peer:
        result = await lookup_synthetic_faq("shipping_demo", httpx_client=peer)
        assert result["source"] == "synthetic:a2a:faq-v1"
        assert result["topic"] == "shipping_demo"
        assert result["answer"].startswith("SYNTHETIC EXAMPLE ONLY:")


@pytest.mark.asyncio
async def test_invalid_topic_never_enters_sdk_network_call(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE,
    ) as peer:
        with pytest.raises(A2AContractError):
            await lookup_synthetic_faq("customer-C-123-private-data", httpx_client=peer)


@pytest.mark.asyncio
async def test_a2a_bearer_rejects_wrong_peer(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE,
        headers={"Authorization": "Bearer " + "c" * 64},
    ) as peer:
        from a2a.client.errors import A2AClientError

        with pytest.raises((A2AClientError, httpx.HTTPError, A2AContractError)):
            await lookup_synthetic_faq("returns_demo", httpx_client=peer)


@pytest.mark.asyncio
async def test_real_independent_process_a2a_rpc_over_loopback(monkeypatch):
    """A separate uvicorn process is discovered over real TCP, not ASGITransport."""
    pytest.importorskip("a2a")
    pytest.importorskip("uvicorn")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("A2A_FAQ_EXPERIMENT_ENABLED", "true")
    monkeypatch.setenv("A2A_FAQ_SHARED_TOKEN", "b" * 64)
    monkeypatch.setenv("A2A_FAQ_ENDPOINT", base_url)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "integrations.a2a_policy.server",
            "--port",
            str(port),
        ],
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as check:
            for _ in range(70):
                if process.poll() is not None:
                    raise AssertionError("A2A peer process failed during startup")
                try:
                    response = await check.get(base_url + "/.well-known/agent-card.json")
                    if response.status_code == 200:
                        ready = True
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            assert ready, "A2A peer did not become ready"
            unauthorized = await check.post(base_url + "/a2a/jsonrpc", json={})
            assert unauthorized.status_code == 401
        result = await lookup_synthetic_faq("returns_demo")
        assert result["topic"] == "returns_demo"
        assert result["source"] == "synthetic:a2a:faq-v1"
        assert result["answer"].startswith("SYNTHETIC EXAMPLE ONLY:")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
