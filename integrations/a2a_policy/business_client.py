"""Main customer-service Agent bridge to the independent policy specialist."""

from __future__ import annotations

import json
import os
from typing import Any

from integrations.a2a_policy.business_contract import (
    CARD_NAME,
    CHANNELS,
    ISSUES,
    SKILL_ID,
    STAGES,
    canonical,
    require_business_config,
    require_private_ca_file,
    unique_object,
    validate_request,
    validate_result,
)
from integrations.a2a_policy.client import delegate_readonly_task
from integrations.a2a_policy.contracts import A2AContractError


async def review_after_sales_procedure(request: dict, *, httpx_client: Any | None = None) -> dict:
    base_url, token, digest = require_business_config()
    request = validate_request(request)
    response = await delegate_readonly_task(
        canonical(request),
        base_url=base_url,
        token=token,
        card_name=CARD_NAME,
        skill_id=SKILL_ID,
        max_chars=4000,
        httpx_client=httpx_client,
        tls_ca_file=require_private_ca_file() if base_url.startswith("https://") else None,
    )
    try:
        result = json.loads(response, object_pairs_hook=unique_object)
        return validate_result(result, request, digest)
    except (TypeError, ValueError):
        raise A2AContractError("After-sales specialist result failed validation") from None


def register_business_a2a_tool(registry: Any) -> bool:
    if os.getenv("A2A_AFTERSALES_ENABLED", "").lower() != "true":
        return False
    require_business_config()
    name = "after_sales_procedure_review"
    if name in registry.list_tools():
        raise A2AContractError("After-sales specialist already registered")
    from core.tool_result_cache import ToolCachePolicy

    registry.register(
        name=name,
        description=(
            "Delegate to the independent after-sales policy specialist to select a customer-facing "
            "procedure from reported issue/channel/stage. Not proof of order facts or refund eligibility; "
            "use authorized ERP and human approval for any actual business action."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "issue": {"type": "string", "enum": list(ISSUES)},
                "channel": {"type": "string", "enum": list(CHANNELS)},
                "stage": {"type": "string", "enum": list(STAGES)},
            },
            "required": ["issue", "channel", "stage"],
        },
        handler=review_after_sales_procedure,
        cache_policy=ToolCachePolicy(enabled=False),
        side_effect=False,
        risk_level="low",
        source="a2a",
    )
    return True
