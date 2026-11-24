"""Optional ReAct registry bridge for the independent A2A specialist.

The reusable SDK client lives in integrations.a2a_policy.client; this module
alone imports the existing ToolResultCache and ToolRegistry ecosystem.
"""

from __future__ import annotations

import os
from typing import Any

from integrations.a2a_policy.client import lookup_synthetic_faq
from integrations.a2a_policy.contracts import (
    A2AContractError,
    require_demo_enabled,
    require_token,
    validate_loopback_base_url,
)


def register_synthetic_a2a_tool(registry: Any) -> bool:
    """Expose exactly one read-only demo tool to the existing ReAct registry.

    Never add a tool to default production mode, and never shadow an ERP tool.
    """
    if os.getenv("A2A_FAQ_EXPERIMENT_ENABLED", "").lower() != "true":
        return False
    require_demo_enabled()
    require_token()
    validate_loopback_base_url(os.getenv("A2A_FAQ_ENDPOINT", ""))
    if "synthetic_a2a_faq_lookup" in registry.list_tools():
        raise A2AContractError("A2A tool name already registered")

    async def handler(args: dict[str, Any]) -> dict[str, str]:
        if not isinstance(args, dict) or set(args) != {"topic"}:
            raise A2AContractError("Only the required synthetic topic parameter is allowed")
        return await lookup_synthetic_faq(args["topic"])

    from core.tool_result_cache import ToolCachePolicy

    registry.register(
        name="synthetic_a2a_faq_lookup",
        description=(
            "Only for local synthetic A2A tests: retrieve an explicitly fictional FAQ "
            "by topic ID returns_demo/shipping_demo/invoice_demo. Not real customer policy."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["returns_demo", "shipping_demo", "invoice_demo"],
                }
            },
            "required": ["topic"],
        },
        handler=handler,
        cache_policy=ToolCachePolicy(enabled=False),
        side_effect=False,
        risk_level="low",
        source="a2a",
    )
    return True
