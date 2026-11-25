"""No-network regression for request-scoped chat attempt accounting."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from langchain_core.messages import HumanMessage

from llm.client import OpenAICompatibleClient
from llm.usage_scope import begin_attempt, collect_usage


def test_nested_and_parallel_scopes_are_isolated():
    async def run():
        async def child():
            attempt = begin_attempt()
            attempt.observe({"usage": {"prompt_tokens": 2, "completion_tokens": 3}})

        with collect_usage() as outer:
            with collect_usage() as inner:
                await asyncio.gather(child(), child())
            assert inner.snapshot()["input_tokens"] == 4
            assert outer.snapshot()["chat_http_attempts"] == 0
        assert begin_attempt() is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": True, "completion_tokens": 2},
        {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 99},
    ],
)
def test_unknown_usage_is_not_zero(usage):
    with collect_usage() as scope:
        attempt = begin_attempt()
        attempt.observe({"usage": usage})
    assert scope.snapshot()["input_tokens"] is None
    assert scope.snapshot()["unknown_usage_attempts"] == 1


@pytest.mark.asyncio
async def test_real_client_counts_retry_and_preserves_unknown_usage():
    client = OpenAICompatibleClient("fixture", "https://fixture.invalid", "fixture")
    client.max_retries = 2
    client.base_delay = 0
    response = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": "fixture"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }
    transport = AsyncMock()
    transport.post.side_effect = [httpx.ConnectError("fixture"), response]
    with patch.object(client, "_get_async_client", return_value=transport), collect_usage() as scope:
        await client.async_invoke([HumanMessage(content="fixture")])
    evidence = scope.snapshot()
    assert evidence["chat_http_attempts"] == 2
    assert evidence["attempts_with_usage"] == 1
    assert evidence["input_tokens"] is None
    assert evidence["output_tokens"] is None


@pytest.mark.asyncio
async def test_real_raw_client_usage_is_collected():
    client = OpenAICompatibleClient("fixture", "https://fixture.invalid", "fixture")
    response = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": "fixture"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }
    transport = AsyncMock()
    transport.post.return_value = response
    with patch.object(client, "_get_async_client", return_value=transport), collect_usage() as scope:
        await client.async_invoke_raw([{"role": "user", "content": "fixture"}])
    assert scope.snapshot()["input_tokens"] == 2
    assert scope.snapshot()["output_tokens"] == 3


def test_route_source_never_guesses_rule_shortcut_from_missing_scripted_counter():
    from evaluation.agent_eval.harness import _route_source

    assert _route_source({"query_type": "general"}, 0) == "unavailable"
    for source in ("rule_shortcut", "llm_arbitrated", "error_fallback", "circuit_breaker_open"):
        assert _route_source({"query_type": "general", "route_source": source}, 0) == source
    assert _route_source({"cached": True, "route_source": "llm_arbitrated"}, 0) == "cached"
