"""F4：per-call claim TTL 下界必须从**实际执行路径**传进 ledger。

历史状态：``execute_idempotent_operation`` 接受 ``min_claim_ttl_seconds`` 并在低于
下界时 fail-closed，但生产路径 ``ToolRegistry._idempotent_operation`` 从未传过它
（缺省 ``None``），因此那句运行期承诺是死的，只有启动期配置校验在兜底。本文件钉住
真实接线：``ToolRegistry`` 读取当前 ``TOOL_EXECUTION_TIMEOUT_SECONDS`` 并把
``min_claim_ttl_seconds(timeout)`` 显式传给 ledger。
"""

from __future__ import annotations

import pytest

from tools.tool_registry import LogicalCall, ToolCachePolicy, ToolRegistry

pytestmark = pytest.mark.unit


@pytest.fixture
def run_context():
    from runtime.context import reset_run_context, set_run_context

    tokens = set_run_context("run-f4", "thread-f4", "task-f4")
    yield
    reset_run_context(tokens)


def _registry(calls: list[dict]) -> ToolRegistry:
    async def _handler(arguments: dict) -> str:
        calls.append(dict(arguments))
        return "written"

    registry = ToolRegistry()
    registry.register(
        name="write_floor",
        description="w",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        side_effect=True,
        cache_policy=ToolCachePolicy(enabled=False),
    )
    return registry


def test_registry_passes_floor_from_tool_timeout(monkeypatch, run_context):
    monkeypatch.setattr("core.config.TOOL_EXECUTION_TIMEOUT_SECONDS", 60.0)

    captured: dict = {}

    async def _fake(*, tool_name, operation_key, run_id, thread_id, arguments, operation, **kw):
        captured.update(kw)
        captured["tool_name"] = tool_name
        return "written"

    import runtime.side_effects as se

    monkeypatch.setattr(se, "execute_idempotent_operation", _fake)

    calls: list[dict] = []
    registry = _registry(calls)

    import asyncio

    asyncio.run(
        registry.execute_raw(
            "write_floor",
            {"amount": 1},
            tool_call_id="call-f4",
            logical_call=LogicalCall(node_id="n#0", agent="a", ordinal=0),
        )
    )
    assert captured["min_claim_ttl_seconds"] == 90.0, "工具超时 60s -> 下界 60+30"


def test_registry_omits_floor_when_timeout_disabled(monkeypatch, run_context):
    monkeypatch.setattr("core.config.TOOL_EXECUTION_TIMEOUT_SECONDS", 0.0)

    captured: dict = {}

    async def _fake(*, tool_name, operation_key, run_id, thread_id, arguments, operation, **kw):
        captured.update(kw)
        return "written"

    import runtime.side_effects as se

    monkeypatch.setattr(se, "execute_idempotent_operation", _fake)

    calls: list[dict] = []
    registry = _registry(calls)

    import asyncio

    asyncio.run(
        registry.execute_raw(
            "write_floor",
            {"amount": 1},
            tool_call_id="call-f4b",
            logical_call=LogicalCall(node_id="n#0", agent="a", ordinal=0),
        )
    )
    assert captured["min_claim_ttl_seconds"] is None, "关闭 per-call 超时 = 无可证明上界"


def test_ledger_fails_closed_when_floor_exceeds_default_claim_ttl(monkeypatch, run_context):
    """运行期强制：工具超时被调大而 claim TTL 没同步 -> 写操作 fail-closed。"""
    import asyncio

    from runtime.errors import PermanentError

    monkeypatch.setattr("core.config.TOOL_EXECUTION_TIMEOUT_SECONDS", 200.0)

    calls: list[dict] = []
    registry = _registry(calls)

    with pytest.raises(PermanentError, match="claim TTL"):
        asyncio.run(
            registry.execute_raw(
                "write_floor",
                {"amount": 1},
                tool_call_id="call-f4c",
                logical_call=LogicalCall(node_id="n#0", agent="a", ordinal=0),
            )
        )
    assert calls == [], "下界不满足时不得执行外部写"
