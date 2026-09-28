from unittest.mock import AsyncMock, MagicMock

import pytest

import agents.base_agent as base_agent_module
from agents.base_agent import BaseAgent
from core.tool_result_cache import (
    InMemoryToolResultCache,
    ToolCachePolicy,
    canonical_tool_cache_key,
)
from core.tool_result_optimizer import ToolResultOptimizer
from core.tool_result_store import InMemoryToolResultStore
from tools.tool_registry import ToolRegistry


class DummyAgent(BaseAgent):
    async def process(self, state):
        return state


def test_canonical_key_is_order_stable_and_scope_sensitive():
    first = canonical_tool_cache_key(
        "query_product", {"b": 2, "a": "中文"}, scope={"user_id": "u1"}
    )
    second = canonical_tool_cache_key(
        "query_product", {"a": "中文", "b": 2}, scope={"user_id": "u1"}
    )
    assert first == second
    assert first != canonical_tool_cache_key(
        "query_product", {"a": "中文", "b": 3}, scope={"user_id": "u1"}
    )
    assert first != canonical_tool_cache_key(
        "query_product", {"a": "中文", "b": 2}, scope={"user_id": "u2"}
    )
    assert "中文" not in first and "u1" not in first


def test_cache_policy_defaults_to_disabled_and_empty_result_is_not_cacheable():
    policy = ToolCachePolicy()
    from core.tool_result_cache import cacheable_result

    assert policy.enabled is False
    assert cacheable_result("查询完成，无结果", policy) is False


@pytest.mark.asyncio
async def test_cache_hit_scope_ttl_and_invalidation():
    now = [100.0]
    cache = InMemoryToolResultCache(clock=lambda: now[0])
    args = {"keyword": "玫瑰"}
    scope = {"user_id": "u1", "session_id": "s1"}
    assert await cache.get("query_product", args, scope=scope) is None
    await cache.set("query_product", args, [{"product_id": "P1"}], scope=scope, ttl_seconds=10)
    assert await cache.get("query_product", {"keyword": "玫瑰"}, scope=scope) == [
        {"product_id": "P1"}
    ]
    assert (
        await cache.get("query_product", args, scope={"user_id": "u2", "session_id": "s1"}) is None
    )
    now[0] = 110.0
    assert await cache.get("query_product", args, scope=scope) is None
    await cache.set("query_product", args, {"x": 1}, scope=scope, ttl_seconds=10)
    await cache.invalidate("query_product", args, scope=scope)
    assert await cache.get("query_product", args, scope=scope) is None


async def _run_two_calls(agent, registry, *, tool_name="query_product", state=None):
    first = MagicMock(
        content="",
        tool_calls=[{"id": "call-1", "name": tool_name, "arguments": {"keyword": "玫瑰"}}],
    )
    second = MagicMock(
        content="",
        tool_calls=[{"id": "call-2", "name": tool_name, "arguments": {"keyword": "玫瑰"}}],
    )
    final = MagicMock(content="完成", tool_calls=[])
    llm = MagicMock()
    llm.async_invoke = AsyncMock(side_effect=[first, second, final])
    agent.set_llm(llm)
    agent.set_tool_registry(registry)
    return await agent._process_with_tools(
        state or {"user_id": "u1", "session_id": "s1", "customer_query": "查产品"},
        "system",
        max_tool_rounds=3,
    )


@pytest.mark.asyncio
async def test_same_safe_tool_call_hits_and_runs_optimizer_after_hit(monkeypatch):
    monkeypatch.setattr(base_agent_module, "TOOL_RESULT_CACHE_ENABLED", True)
    calls = 0

    async def handler(arguments):
        nonlocal calls
        calls += 1
        return [{"product_id": "P1", "name": "玫瑰精华", "debug": "omit"}]

    registry = ToolRegistry()
    registry.register(
        "query_product", "read", {}, handler, ToolCachePolicy(enabled=True, ttl_seconds=30)
    )
    agent = DummyAgent(name="cache-agent", role="test", expertise=["test"])
    agent.set_tool_result_optimizer(ToolResultOptimizer(enabled=True))
    agent.set_tool_result_cache(InMemoryToolResultCache())
    result = await _run_two_calls(agent, registry)
    assert result["response"] == "完成"
    assert calls == 1


@pytest.mark.asyncio
async def test_disabled_or_side_effect_policy_never_reuses_execution_result(monkeypatch):
    monkeypatch.setattr(base_agent_module, "TOOL_RESULT_CACHE_ENABLED", True)
    calls = 0

    async def handler(arguments):
        nonlocal calls
        calls += 1
        return {"ok": True}

    registry = ToolRegistry()
    registry.register("send_email", "side effect", {}, handler, ToolCachePolicy(enabled=False))
    agent = DummyAgent(name="unsafe-cache-agent", role="test", expertise=["test"])
    agent.set_tool_result_optimizer(ToolResultOptimizer(enabled=True))
    agent.set_tool_result_cache(InMemoryToolResultCache())
    await _run_two_calls(agent, registry, tool_name="send_email")
    assert calls == 2


@pytest.mark.asyncio
async def test_error_result_is_not_cached_and_cache_failure_is_fail_soft(monkeypatch):
    monkeypatch.setattr(base_agent_module, "TOOL_RESULT_CACHE_ENABLED", True)
    calls = 0

    async def failing_handler(arguments):
        nonlocal calls
        calls += 1
        raise TimeoutError("temporary ERP failure")

    registry = ToolRegistry()
    registry.register(
        "query_product", "read", {}, failing_handler, ToolCachePolicy(enabled=True, ttl_seconds=30)
    )
    agent = DummyAgent(name="failure-cache-agent", role="test", expertise=["test"])
    agent.set_tool_result_optimizer(ToolResultOptimizer(enabled=True))

    class BrokenCache:
        async def get(self, *args, **kwargs):
            raise RuntimeError("redis unavailable")

        async def set(self, *args, **kwargs):
            raise RuntimeError("redis unavailable")

    agent.set_tool_result_cache(BrokenCache())
    await _run_two_calls(agent, registry)
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_hit_large_result_still_offloads_after_optimization(monkeypatch):
    monkeypatch.setattr(base_agent_module, "TOOL_RESULT_CACHE_ENABLED", True)
    raw = [{"product_id": "P1", "description": "large" * 100}]
    calls = 0

    async def handler(arguments):
        nonlocal calls
        calls += 1
        return raw

    registry = ToolRegistry()
    registry.register(
        "query_product", "read", {}, handler, ToolCachePolicy(enabled=True, ttl_seconds=30)
    )
    store = InMemoryToolResultStore()
    agent = DummyAgent(name="cache-offload-agent", role="test", expertise=["test"])
    agent.set_tool_result_optimizer(
        ToolResultOptimizer(enabled=True, store=store, offload_enabled=True, offload_min_tokens=1)
    )
    agent.set_tool_result_cache(InMemoryToolResultCache())
    result = await _run_two_calls(agent, registry)
    assert result["response"] == "完成"
    assert calls == 1
