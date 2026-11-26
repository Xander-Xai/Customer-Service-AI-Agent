"""Independent A2A after-sales specialist with private operator-approved rules."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Part,
    SecurityRequirement,
    SecurityScheme,
    StringList,
    Task,
    TaskState,
    TaskStatus,
)

from integrations.a2a_policy.business_contract import (
    CARD_NAME,
    SKILL_ID,
    PolicyCatalog,
    canonical,
    require_business_config,
    unique_object,
)
from integrations.a2a_policy.contracts import A2AContractError
from integrations.a2a_policy.server import build_peer_app


class AfterSalesExecutor(AgentExecutor):
    def __init__(self, catalog: PolicyCatalog):
        self.catalog = catalog

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.message or not context.task_id or not context.context_id:
            raise A2AContractError("Missing task identity")
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message],
            )
        )
        updater = TaskUpdater(event_queue, task_id=context.task_id, context_id=context.context_id)
        try:
            payload = context.get_user_input()
            if len(payload) > 512:
                raise A2AContractError("Descriptor too large")
            result = self.catalog.evaluate(json.loads(payload, object_pairs_hook=unique_object))
        except (TypeError, ValueError):
            await updater.update_status(TaskState.TASK_STATE_REJECTED)
            return
        await updater.add_artifact(
            parts=[Part(text=canonical(result))], name="policy-review", last_chunk=True
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id or "",
            context_id=context.context_id or "",
        )
        await updater.cancel()


def build_business_app(catalog_path: Path):
    base_url, token, digest = require_business_config()
    catalog = PolicyCatalog(catalog_path, digest)
    card = AgentCard(
        name=CARD_NAME,
        description="Independent after-sales procedure specialist; no refund or order authority.",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC", protocol_version="1.0", url=base_url + "/a2a/jsonrpc"
            )
        ],
        skills=[
            AgentSkill(
                id=SKILL_ID,
                name="After-sales procedure review",
                description=(
                    "Select an approved current procedure by issue, purchase channel and reported stage."
                ),
                tags=["after-sales", "read-only", "policy"],
            )
        ],
        security_schemes={
            "peer_bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer")
            )
        },
        security_requirements=[SecurityRequirement(schemes={"peer_bearer": StringList(list=[])})],
    )
    return build_peer_app(card, AfterSalesExecutor(catalog), token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--listen-port", type=int, default=41243)
    parser.add_argument("--authorized-customer-disclosure", action="store_true", required=True)
    args = parser.parse_args()
    app = build_business_app(args.catalog)
    from urllib.parse import urlsplit

    import uvicorn

    base_url, _, _ = require_business_config()
    # Private HTTPS is terminated by the operator-managed reverse proxy. The
    # explicitly opted-in local Compose profile binds to its private bridge;
    # that service has no published host port and accepts no industrial config.
    port = args.listen_port if base_url.startswith("https://") else urlsplit(base_url).port
    if port is None or not 1 <= port <= 65535:
        parser.error("invalid peer listen port")
    internal_http = os.getenv("A2A_AFTERSALES_INTERNAL_HTTP", "").lower() == "true"
    uvicorn.run(
        app, host="0.0.0.0" if internal_http else "127.0.0.1", port=port, proxy_headers=False
    )


if __name__ == "__main__":
    main()
