"""Real A2A SDK round trip to the versioned after-sales rule engine."""

import hashlib
import json

import httpx
import pytest

from integrations.a2a_policy.business_client import review_after_sales_procedure
from integrations.a2a_policy.business_contract import PolicyCatalog
from integrations.a2a_policy.contracts import A2AContractError

BASE = "http://127.0.0.1:41243"
REQUEST = {"issue": "damaged", "channel": "online", "stage": "received_unopened"}


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    raw = json.dumps(
        {
            "schema": "after-sales-procedures/v1",
            "rules": [
                {
                    **REQUEST,
                    "route": "collect_evidence",
                    "procedure": "CI fixture: collect packaging condition for staff review.",
                    "source_id": "ci-policy",
                    "revision": "r1",
                    "valid_from": "2020-01-01",
                    "valid_until": "2099-01-01",
                    "audience": "customer_public",
                }
            ],
        }
    ).encode()
    path = tmp_path / "policy.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("A2A_AFTERSALES_ENABLED", "true")
    monkeypatch.setenv("A2A_AFTERSALES_TOKEN", "b" * 64)
    monkeypatch.setenv("A2A_AFTERSALES_ENDPOINT", BASE)
    monkeypatch.setenv("A2A_AFTERSALES_POLICY_SHA256", digest)
    return path, digest


def test_rule_engine_provenance_and_fail_closed(catalog):
    path, digest = catalog
    engine = PolicyCatalog(path, digest)
    result = engine.evaluate(REQUEST)
    assert result["source_id"] == "ci-policy"
    assert result["execution_authorized"] is False
    assert result["refund_eligibility_verified"] is False
    assert engine.evaluate({**REQUEST, "stage": "unknown"})["status"] == "REVIEW_REQUIRED"
    with pytest.raises(A2AContractError):
        engine.evaluate({**REQUEST, "customer_id": "private"})
    with pytest.raises(A2AContractError):
        PolicyCatalog(path, "a" * 64)


@pytest.mark.asyncio
async def test_actual_sdk_policy_review_roundtrip(catalog, monkeypatch):
    pytest.importorskip("a2a")
    from integrations.a2a_policy.business_server import build_business_app

    path, digest = catalog
    app = build_business_app(path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE,
        headers={"Authorization": "Bearer " + "b" * 64},
    ) as client:
        result = await review_after_sales_procedure(REQUEST, httpx_client=client)
        assert result["route"] == "collect_evidence"
        assert result["policy_sha256"] == digest
        assert result["execution_authorized"] is False
        assert (
            await review_after_sales_procedure(
                {**REQUEST, "channel": "unknown"}, httpx_client=client
            )
        )["status"] == "REVIEW_REQUIRED"
        monkeypatch.setenv("A2A_AFTERSALES_POLICY_SHA256", "a" * 64)
        with pytest.raises(A2AContractError):
            await review_after_sales_procedure(REQUEST, httpx_client=client)


@pytest.mark.asyncio
async def test_peer_rejects_actual_oversized_chunked_rpc_before_sdk(catalog):
    pytest.importorskip("a2a")
    from integrations.a2a_policy.business_server import build_business_app

    path, _ = catalog

    async def chunks():
        yield b"x" * 4096
        yield b"x" * 4096
        yield b"x"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_business_app(path)),
        base_url=BASE,
        headers={"Authorization": "Bearer " + "b" * 64},
    ) as peer:
        response = await peer.post("/a2a/jsonrpc", content=chunks())
        assert response.status_code == 413
        response = await peer.post(
            "/a2a/jsonrpc", content=b"{}", headers={"Content-Length": "1"}
        )
        assert response.status_code == 400
        response = await peer.post(
            "/a2a/jsonrpc",
            content=b"{}",
            headers=[("Authorization", "Bearer " + "b" * 64), ("Authorization", "Bearer other")],
        )
        assert response.status_code == 401
        result = await review_after_sales_procedure(REQUEST, httpx_client=peer)
        assert result["route"] == "collect_evidence"
