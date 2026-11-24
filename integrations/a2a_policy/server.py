"""Standalone A2A 1.0 JSON-RPC specialist for fictional customer-service FAQ topics.

No import/mount in api.main, no ERP, no knowledge DB, no external model, no network
tools, no payment/order authority. Requires explicit local/test opt-in + bearer.
"""

from __future__ import annotations

import argparse
import hmac

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
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
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from integrations.a2a_policy.contracts import (
    A2AContractError,
    require_demo_enabled,
    require_token,
    synthetic_policy_answer,
    validate_loopback_base_url,
)


class SyntheticFAQExecutor(AgentExecutor):
    """A2A executor; every task is a read-only, fixed-identifier lookup."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.message or not context.task_id or not context.context_id:
            raise A2AContractError("Missing A2A task identity or input")
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
            # No untrusted message may reach an ERP API, shell, LLM or data store.
            topic = context.get_user_input()
            answer = synthetic_policy_answer(topic)
        except A2AContractError:
            await updater.update_status(TaskState.TASK_STATE_REJECTED)
            return
        await updater.add_artifact(
            parts=[Part(text=answer)],
            name="synthetic-faq-result",
            last_chunk=True,
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # This service completes synchronously and offers no cancelable writes.
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id or "",
            context_id=context.context_id or "",
        )
        await updater.cancel()


def build_app(base_url: str = "http://127.0.0.1:41243") -> FastAPI:
    """Build an isolated A2A app; the main customer service API is unaffected."""
    require_demo_enabled()
    secret = require_token()
    base_url = validate_loopback_base_url(base_url)

    card = AgentCard(
        name="Synthetic Customer FAQ Specialist",
        description="Local-only fictional read-only FAQ specialist; no ERP/customer data.",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url=f"{base_url}/a2a/jsonrpc",
            )
        ],
        skills=[
            AgentSkill(
                id="synthetic_faq_readonly",
                name="Fictional FAQ Lookup",
                description="Retrieve one fixed synthetic topic with no private data or side effects.",
                tags=["synthetic", "faq", "read-only"],
                examples=["returns_demo", "shipping_demo", "invoice_demo"],
            )
        ],
        security_schemes={
            "demo_bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="Bearer",
                    description="Locally configured A2A_FAQ_SHARED_TOKEN (not shared with user prompts).",
                )
            )
        },
        security_requirements=[
            SecurityRequirement(schemes={"demo_bearer": StringList(list=[])})
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=SyntheticFAQExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url="/a2a/jsonrpc"),
        ],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def require_peer_auth(request: Request, call_next):
        # The public card is intentionally non-secret, and describes Bearer auth.
        if request.method == "GET" and request.url.path == "/.well-known/agent-card.json":
            return await call_next(request)
        if request.method != "POST" or request.url.path != "/a2a/jsonrpc":
            return JSONResponse({"detail": "Not found"}, status_code=404)
        auth = request.headers.get("authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix) or not hmac.compare_digest(auth[len(prefix):], secret):
            return JSONResponse(
                {"detail": "A2A peer authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        declared_length = request.headers.get("content-length")
        if declared_length:
            try:
                if int(declared_length) > 8192:
                    return JSONResponse({"detail": "Payload too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid request size"}, status_code=400)
        return await call_next(request)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Local synthetic A2A 1.0 FAQ sidecar")
    parser.add_argument("--port", type=int, default=41243)
    args = parser.parse_args()
    require_demo_enabled()
    import uvicorn

    base_url = f"http://127.0.0.1:{args.port}"
    uvicorn.run(build_app(base_url), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
