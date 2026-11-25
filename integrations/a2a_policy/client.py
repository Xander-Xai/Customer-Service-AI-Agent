"""Opt-in A2A 1.0 peer adapter for the read-only synthetic FAQ specialist.

Strict boundary: never send user utterances, messages, tool observations, ERP
records or session memory. Only a fixed enum-like synthetic topic is transmitted.
The remote Agent Card is checked before constructing the SDK client to avoid
redirecting peer credentials to an advertised arbitrary destination.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import uuid
from typing import Any

from integrations.a2a_policy.contracts import (
    A2AContractError,
    require_demo_enabled,
    require_token,
    synthetic_policy_answer,
    validate_loopback_base_url,
    validate_private_https_base_url,
    validate_topic,
)

A2A_DEADLINE_SECONDS = 3.0


async def lookup_synthetic_faq(
    topic: str,
    *,
    httpx_client: Any | None = None,
) -> dict[str, str]:
    """Real A2A SDK Agent Card discovery + SendMessage across an HTTP boundary.

    Test can inject an in-memory ASGI HTTPX transport. Production customer
    requests never enter this experiment unless an operator explicitly opts in.
    """
    require_demo_enabled()
    token = require_token()
    topic = validate_topic(topic)
    base_url = validate_loopback_base_url(os.getenv("A2A_FAQ_ENDPOINT", ""))
    answer = await delegate_readonly_task(
        topic,
        base_url=base_url,
        token=token,
        card_name="Synthetic Customer FAQ Specialist",
        skill_id="synthetic_faq_readonly",
        max_chars=800,
        httpx_client=httpx_client,
    )
    if answer != synthetic_policy_answer(topic):
        raise A2AContractError("Remote A2A result does not match the synthetic topic")
    return {"topic": topic, "source": "synthetic:a2a:faq-v1", "answer": answer}


async def delegate_readonly_task(
    payload: str,
    *,
    base_url: str,
    token: str,
    card_name: str,
    skill_id: str,
    max_chars: int,
    httpx_client: Any | None = None,
    tls_ca_file: str | None = None,
    allow_internal_http: bool = False,
) -> str:
    """Shared SDK transport; caller defines the trusted peer and result contract."""
    if tls_ca_file:
        base_url = validate_private_https_base_url(base_url)
    elif allow_internal_http:
        if base_url != "http://a2a-peer:41243":
            raise A2AContractError("Internal HTTP peer must use the isolated a2a-peer service")
    else:
        base_url = validate_loopback_base_url(base_url)
    expected_rpc_url = base_url + "/a2a/jsonrpc"

    import httpx
    from a2a.client import A2ACardResolver, ClientConfig, create_client
    from a2a.helpers import get_artifact_text
    from a2a.types import Message, Part, Role, SendMessageRequest, TaskState

    async def call(peer_http: httpx.AsyncClient) -> str:
        resolver = A2ACardResolver(peer_http, base_url)
        card = await resolver.get_agent_card()
        if (
            card.name != card_name
            or len(card.supported_interfaces) != 1
            or card.supported_interfaces[0].protocol_binding != "JSONRPC"
            or card.supported_interfaces[0].protocol_version != "1.0"
            or card.supported_interfaces[0].url != expected_rpc_url
            or len(card.skills) != 1
            or card.skills[0].id != skill_id
        ):
            raise A2AContractError("Remote A2A Agent Card is not the allowlisted peer")
        peer = await create_client(
            card,
            client_config=ClientConfig(
                streaming=False,
                polling=False,
                supported_protocol_bindings=["JSONRPC"],
                httpx_client=peer_http,
            ),
        )
        try:
            request = SendMessageRequest(
                message=Message(
                    role=Role.ROLE_USER,
                    message_id=str(uuid.uuid4()),
                    context_id=str(uuid.uuid4()),
                    parts=[Part(text=payload)],
                )
            )
            finished = False
            observed: list[str] = []
            async for event in peer.send_message(request):
                if event.HasField("task"):
                    task = event.task
                    if task.status.state == TaskState.TASK_STATE_COMPLETED:
                        finished = True
                    elif task.status.state in {
                        TaskState.TASK_STATE_FAILED,
                        TaskState.TASK_STATE_REJECTED,
                        TaskState.TASK_STATE_CANCELED,
                    }:
                        raise A2AContractError("Remote A2A task was not completed")
                    observed.extend(get_artifact_text(a) for a in task.artifacts)
                elif event.HasField("artifact_update"):
                    observed.append(get_artifact_text(event.artifact_update.artifact))
                elif event.HasField("status_update"):
                    state = event.status_update.status.state
                    if state == TaskState.TASK_STATE_COMPLETED:
                        finished = True
                    elif state in {
                        TaskState.TASK_STATE_FAILED,
                        TaskState.TASK_STATE_REJECTED,
                        TaskState.TASK_STATE_CANCELED,
                    }:
                        raise A2AContractError("Remote A2A task was not completed")
                else:
                    raise A2AContractError("Unexpected A2A response event")
            # A completed task with zero or multiple answers is not a verified
            # single-result read. Never substitute fallback hallucinated text.
            if not finished or len(observed) != 1 or not 0 < len(observed[0]) <= max_chars:
                raise A2AContractError("Remote A2A task result contract failed")
            return observed[0]
        finally:
            await peer.close()

    async def exchange() -> str:
        if httpx_client is not None:
            return await call(httpx_client)
        # Private HTTPS verifies the configured DNS name using only the supplied CA.
        # Local protocol fixtures retain their existing loopback HTTP boundary.
        verify = ssl.create_default_context(cafile=tls_ca_file) if tls_ca_file else True
        async with httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(A2A_DEADLINE_SECONDS),
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            follow_redirects=False,
        ) as client:
            return await call(client)

    # HTTP timeouts alone apply per operation, not to the complete delegation.
    # This is a cooperative deadline, including discovery and SDK cleanup.
    try:
        return await asyncio.wait_for(exchange(), timeout=A2A_DEADLINE_SECONDS)
    except (httpx.HTTPError, asyncio.TimeoutError, OSError):
        raise A2AContractError("A2A peer unavailable") from None
