"""Opt-in A2A 1.0 peer adapter for the read-only synthetic FAQ specialist.

Strict boundary: never send user utterances, messages, tool observations, ERP
records or session memory. Only a fixed enum-like synthetic topic is transmitted.
The remote Agent Card is checked before constructing the SDK client to avoid
redirecting peer credentials to an advertised arbitrary destination.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from integrations.a2a_policy.contracts import (
    A2AContractError,
    require_demo_enabled,
    require_token,
    validate_loopback_base_url,
    validate_topic,
)


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
    expected_rpc_url = base_url + "/a2a/jsonrpc"

    import httpx
    from a2a.client import A2ACardResolver, ClientConfig, create_client
    from a2a.helpers import get_artifact_text
    from a2a.types import Message, Part, Role, SendMessageRequest, TaskState

    async def call(peer_http: httpx.AsyncClient) -> dict[str, str]:
        resolver = A2ACardResolver(peer_http, base_url)
        card = await resolver.get_agent_card()
        if (
            card.name != "Synthetic Customer FAQ Specialist"
            or len(card.supported_interfaces) != 1
            or card.supported_interfaces[0].protocol_binding != "JSONRPC"
            or card.supported_interfaces[0].protocol_version != "1.0"
            or card.supported_interfaces[0].url != expected_rpc_url
            or len(card.skills) != 1
            or card.skills[0].id != "synthetic_faq_readonly"
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
                    parts=[Part(text=topic)],
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
            if not finished or len(observed) != 1 or not 0 < len(observed[0]) <= 800:
                raise A2AContractError("Remote A2A task result contract failed")
            if not observed[0].startswith("SYNTHETIC EXAMPLE ONLY:"):
                raise A2AContractError("Remote A2A result lacks synthetic provenance")
            return {"topic": topic, "source": "synthetic:a2a:faq-v1", "answer": observed[0]}
        finally:
            await peer.close()

    if httpx_client is not None:
        return await call(httpx_client)
    # Do not follow redirects, proxy environment or remote DNS records. Peer
    # location is a locally configured literal loopback host:port.
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.0),
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            follow_redirects=False,
        ) as client:
            return await call(client)
    except (httpx.HTTPError, TimeoutError, OSError):
        raise A2AContractError("Synthetic A2A peer unavailable") from None
