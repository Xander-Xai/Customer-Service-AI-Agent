import pytest

from core.tool_result_optimizer import ToolResultOptimizer
from core.tool_result_store import InMemoryToolResultStore


class FakeSummarizer:
    async def summarize(self, content):
        return "semantic preview"


class FailingSummarizer:
    async def summarize(self, content):
        raise TimeoutError("timed out")


@pytest.mark.asyncio
async def test_semantic_summary_is_opt_in_and_falls_back():
    raw = [{"order_id": str(i), "description": "large" * 100} for i in range(20)]
    store = InMemoryToolResultStore()
    disabled = ToolResultOptimizer(enabled=True, store=store, offload_enabled=True, offload_min_tokens=1, summarizer=FakeSummarizer())
    disabled_result = await disabled.optimize_async("query_order", raw, scope={"user_id": "u"})
    assert "semantic preview" not in disabled_result.content

    enabled = ToolResultOptimizer(enabled=True, store=store, offload_enabled=True, offload_min_tokens=1,
                                  semantic_summary_enabled=True, summarizer=FakeSummarizer())
    enabled_result = await enabled.optimize_async("query_order", raw, scope={"user_id": "u"})
    assert "semantic preview" in enabled_result.content

    failing = ToolResultOptimizer(enabled=True, store=store, offload_enabled=True, offload_min_tokens=1,
                                  semantic_summary_enabled=True, summarizer=FailingSummarizer())
    failing_result = await failing.optimize_async("query_order", raw, scope={"user_id": "u"})
    assert "result_offloaded" in failing_result.content
