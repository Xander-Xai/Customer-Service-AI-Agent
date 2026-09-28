import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from core.tool_result_optimizer import (
    ToolResultOptimizer,
    ToolResultPolicy,
    compact_old_tool_messages,
)


def test_structured_result_filters_fields_deduplicates_and_limits_items():
    optimizer = ToolResultOptimizer()
    result = optimizer.optimize(
        "query_order",
        [
            {"order_id": "O1", "status": "shipped", "debug": "secret", "empty": ""},
            {"order_id": "O1", "status": "shipped", "debug": "secret"},
            {"order_id": "O2", "status": "pending", "debug": "secret"},
        ],
        ToolResultPolicy(
            max_tokens=None,
            max_items=2,
            include_fields={"order_id", "status", "debug", "empty"},
            exclude_fields={"debug"},
            strategy="top_k",
        ),
    )

    payload = json.loads(result.content)
    assert payload == [
        {"order_id": "O1", "status": "shipped"},
        {"order_id": "O2", "status": "pending"},
    ]
    assert result.truncated is True
    assert result.item_count_before == 3
    assert result.item_count_after == 2
    assert result.optimized_token_estimate < result.raw_token_estimate


def test_list_field_whitelist_applies_to_each_record():
    result = ToolResultOptimizer().optimize(
        "query_inventory",
        [{"product_id": "P1", "stock": 3, "internal": "x"}],
        ToolResultPolicy(include_fields={"product_id", "stock"}),
    )
    assert json.loads(result.content) == [{"product_id": "P1", "stock": 3}]


def test_json_string_result_is_structured_before_fallback():
    result = ToolResultOptimizer().optimize(
        "query_order",
        '{"order_id":"O1","status":"shipped","debug":"secret"}',
        ToolResultPolicy(include_fields={"order_id", "status"}),
    )
    assert json.loads(result.content) == {"order_id": "O1", "status": "shipped"}


def test_dict_keeps_continuation_ids_and_supports_chinese_estimate():
    optimizer = ToolResultOptimizer()
    result = optimizer.optimize(
        "query_product",
        {
            "product_id": "P001",
            "name": "烟酰胺精华",
            "description": "适合混合性肌肤使用" * 20,
            "raw_payload": "不要暴露",
        },
        ToolResultPolicy(
            max_tokens=25,
            include_fields={"product_id", "name", "description"},
            preserve_recent=1,
            strategy="structured",
        ),
    )

    payload = json.loads(result.content)
    assert payload["product_id"] == "P001"
    assert payload["name"] == "烟酰胺精华"
    assert result.raw_token_estimate > 0
    assert result.optimized_token_estimate <= 25
    assert result.truncated is True


@pytest.mark.parametrize("value", [None, "", [], {}])
def test_empty_results_are_safe(value):
    result = ToolResultOptimizer().optimize(
        "query_customer", value, ToolResultPolicy(max_tokens=20)
    )
    assert result.content
    assert result.optimized_token_estimate <= 20


def test_string_result_is_truncated_without_claiming_exact_model_tokens():
    result = ToolResultOptimizer().optimize(
        "unknown_tool", "中文结果。" * 200, ToolResultPolicy(max_tokens=12)
    )
    assert result.truncated is True
    assert result.content.endswith("…")
    assert result.optimized_token_estimate <= 12


def test_disabled_optimizer_preserves_raw_content():
    raw = {"order_id": "O1", "debug": "keep me", "items": list(range(20))}
    result = ToolResultOptimizer(enabled=False).optimize(
        "query_order", raw, ToolResultPolicy(max_tokens=1, max_items=1)
    )
    assert result.content == json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    assert result.truncated is False


def test_old_tool_messages_are_compacted_without_breaking_call_pairing():
    messages = [
        AIMessage(content="", tool_calls=[{"id": "call-1", "name": "query_order", "args": {}}]),
        ToolMessage(content='{"order_id":"O1","status":"shipped"}', tool_call_id="call-1"),
        AIMessage(content="", tool_calls=[{"id": "call-2", "name": "query_customer", "args": {}}]),
        ToolMessage(content='{"customer_id":"C1","name":"王女士"}', tool_call_id="call-2"),
    ]
    compacted = compact_old_tool_messages(messages, preserve_recent=1)

    assert len(compacted) == len(messages)
    assert compacted[1].tool_call_id == "call-1"
    assert compacted[3].tool_call_id == "call-2"
    assert "older tool result compacted" in compacted[1].content
    assert json.loads(compacted[3].content)["customer_id"] == "C1"
