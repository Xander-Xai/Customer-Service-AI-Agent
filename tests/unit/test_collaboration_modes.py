"""
测试 collaboration/modes.py — 5 种协作模式覆盖率补齐
目标：Sequential / Parallel / Consultation / Hierarchical / ReAct 全分支覆盖
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from collaboration.modes import (
    ConsultationMode,
    HierarchicalMode,
    ParallelMode,
    ReActMode,
    SequentialMode,
)
from core.message_bus import MessageBus
from core.shared_blackboard import SharedBlackboard

# ── 辅助工具 ──────────────────────────────────────────────────────


def _mock_agent(response="ok", side_effect=None):
    """创建可复用的 mock agent"""
    agent = AsyncMock()
    if side_effect:
        agent.process_with_retry = AsyncMock(side_effect=side_effect)
    else:
        agent.process_with_retry = AsyncMock(return_value={"response": response})
    return agent


def _mock_bus():
    bus = AsyncMock(spec=MessageBus)
    bus.publish = AsyncMock()
    return bus


def _mock_bb():
    bb = AsyncMock(spec=SharedBlackboard)
    bb.write = AsyncMock()
    bb.read_prefix = AsyncMock(return_value={})
    return bb


def _state(query="你好"):
    return {"customer_query": query, "session_id": "test-session"}


# ── SequentialMode ────────────────────────────────────────────────


class TestSequentialMode:

    @pytest.mark.asyncio
    async def test_agent_found_returns_result(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = SequentialMode(bus=bus, bb=bb)
        agents = {"general_agent": _mock_agent("你好！")}

        result = await mode.execute(agents, _state(), {"primary_agent": "general_agent"})

        assert result["response"] == "你好！"
        assert result["mode"] == "sequential"
        assert result["agents_used"] == ["general_agent"]
        assert "elapsed" in result

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        mode = SequentialMode()
        result = await mode.execute({}, _state(), {"primary_agent": "missing_agent"})

        assert "not found" in result["response"]
        assert result["mode"] == "sequential"
        assert result["agents_used"] == []

    @pytest.mark.asyncio
    async def test_default_agent_name(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = SequentialMode(bus=bus, bb=bb)
        agents = {"general_agent": _mock_agent("默认回复")}

        result = await mode.execute(agents, _state(), {})

        assert result["agents_used"] == ["general_agent"]

    @pytest.mark.asyncio
    async def test_publishes_start_and_complete_events(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = SequentialMode(bus=bus, bb=bb)
        agents = {"general_agent": _mock_agent()}

        await mode.execute(agents, _state(), {"primary_agent": "general_agent"})

        assert bus.publish.call_count == 2  # start + complete


# ── ParallelMode ─────────────────────────────────────────────────


class TestParallelMode:

    @pytest.mark.asyncio
    async def test_multiple_agents_aggregated(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ParallelMode(bus=bus, bb=bb, max_workers=3)
        agents = {
            "product_agent": _mock_agent("产品信息"),
            "tech_agent": _mock_agent("技术支持"),
        }

        result = await mode.execute(
            agents, _state(), {"agent_list": ["product_agent", "tech_agent"]}
        )

        assert result["mode"] == "parallel"
        assert "product_agent" in result["agents_used"]
        assert "tech_agent" in result["agents_used"]
        assert "产品信息" in result["response"]
        assert "技术支持" in result["response"]

    @pytest.mark.asyncio
    async def test_missing_agent_skipped(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ParallelMode(bus=bus, bb=bb)
        agents = {"product_agent": _mock_agent("产品")}

        result = await mode.execute(
            agents, _state(), {"agent_list": ["product_agent", "missing_agent"]}
        )

        assert "product_agent" in result["agents_used"]
        assert "not found" in result["response"] or "产品" in result["response"]

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ParallelMode(bus=bus, bb=bb)
        mode._PARALLEL_TIMEOUT = 0.001  # 极短超时

        async def _slow_agent(state):
            await asyncio.sleep(10)
            return {"response": "slow"}

        agent = AsyncMock()
        agent.process_with_retry = _slow_agent
        agents = {"slow_agent": agent}

        result = await mode.execute(
            agents, _state(), {"agent_list": ["slow_agent"]}
        )

        assert result["mode"] == "parallel"
        # 超时后 gather 收到 TimeoutError，results 为空，聚合为默认消息
        assert result["response"] == "无可用 Agent 响应"

    @pytest.mark.asyncio
    async def test_empty_agent_list(self):
        mode = ParallelMode()
        result = await mode.execute({}, _state(), {"agent_list": []})

        assert result["mode"] == "parallel"
        assert result["agents_used"] == []

    @pytest.mark.asyncio
    async def test_agent_exception_included_in_response(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ParallelMode(bus=bus, bb=bb)
        agent = _mock_agent(side_effect=RuntimeError("boom"))
        agents = {"bad_agent": agent}

        result = await mode.execute(
            agents, _state(), {"agent_list": ["bad_agent"]}
        )

        assert "error" in result["response"].lower() or "boom" in result["response"]

    @pytest.mark.asyncio
    async def test_writes_to_blackboard(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ParallelMode(bus=bus, bb=bb)
        agents = {"product_agent": _mock_agent("产品结果")}

        await mode.execute(
            agents, _state(), {"agent_list": ["product_agent"]}
        )

        bb.write.assert_called_once()
        call_args = bb.write.call_args
        assert "parallel.result.product_agent" in call_args[0][0]


# ── ConsultationMode ─────────────────────────────────────────────


class TestConsultationMode:

    @pytest.mark.asyncio
    async def test_primary_not_found(self):
        mode = ConsultationMode()
        result = await mode.execute({}, _state(), {"primary_agent": "missing"})

        assert "not found" in result["response"]
        assert result["agents_used"] == []

    @pytest.mark.asyncio
    async def test_no_consultees_calls_primary_only(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ConsultationMode(bus=bus, bb=bb)
        agents = {"general_agent": _mock_agent("主回答")}

        result = await mode.execute(
            agents, _state(), {"primary_agent": "general_agent", "consult_agents": []}
        )

        assert result["response"] == "主回答"
        assert result["agents_used"] == ["general_agent"]

    @pytest.mark.asyncio
    async def test_with_consultees_calls_all(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ConsultationMode(bus=bus, bb=bb)
        agents = {
            "product_agent": _mock_agent("产品主答"),
            "tech_agent": _mock_agent("技术补充"),
        }

        result = await mode.execute(
            agents,
            _state("这个产品有问题"),
            {"primary_agent": "product_agent", "consult_agents": ["tech_agent"]},
        )

        assert "产品主答" in result["response"]
        assert "product_agent" in result["agents_used"]
        assert "tech_agent" in result["agents_used"]

    @pytest.mark.asyncio
    async def test_consultee_timeout_proceeds_with_primary(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ConsultationMode(bus=bus, bb=bb, consult_timeout=0.001)

        async def _slow(state):
            await asyncio.sleep(10)
            return {"response": "slow"}

        slow_agent = AsyncMock()
        slow_agent.process_with_retry = _slow
        primary = _mock_agent("主回答")

        agents = {"product_agent": primary, "tech_agent": slow_agent}

        result = await mode.execute(
            agents, _state("问题"),
            {"primary_agent": "product_agent", "consult_agents": ["tech_agent"]},
        )

        assert result["response"] == "主回答"
        assert "product_agent" in result["agents_used"]

    @pytest.mark.asyncio
    async def test_blackboard_data_injected(self):
        bus, bb = _mock_bus(), _mock_bb()
        bb.read_prefix = AsyncMock(
            return_value={"erp.result": {"response": "ERP 补充信息"}}
        )
        mode = ConsultationMode(bus=bus, bb=bb)
        agents = {"product_agent": _mock_agent("主回答")}

        result = await mode.execute(
            agents, _state("查订单"),
            {"primary_agent": "product_agent", "consult_agents": []},
        )

        assert result["response"] == "主回答"

    @pytest.mark.asyncio
    async def test_consultee_not_found_skipped(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ConsultationMode(bus=bus, bb=bb)
        agents = {"product_agent": _mock_agent("主答")}

        result = await mode.execute(
            agents, _state("问题"),
            {"primary_agent": "product_agent", "consult_agents": ["missing_agent"]},
        )

        assert result["response"] == "主答"


# ── HierarchicalMode ──────────────────────────────────────────────


class TestHierarchicalMode:

    @pytest.mark.asyncio
    async def test_coordinator_not_found(self):
        mode = HierarchicalMode()
        result = await mode.execute({}, _state(), {"coordinator": "missing"})

        assert "not found" in result["response"]
        assert result["agents_used"] == []

    @pytest.mark.asyncio
    async def test_subtasks_executed_and_summarized(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = HierarchicalMode(bus=bus, bb=bb)
        agents = {
            "general_agent": _mock_agent("汇总结果"),
            "complaint_agent": _mock_agent("投诉处理"),
            "product_agent": _mock_agent("产品检查"),
        }

        result = await mode.execute(
            agents, _state("投诉产品质量"),
            {
                "coordinator": "general_agent",
                "sub_tasks": {"complaint_agent": "处理投诉", "product_agent": "检查产品"},
            },
        )

        assert result["response"] == "汇总结果"
        assert result["mode"] == "hierarchical"
        assert "general_agent" in result["agents_used"]
        assert "complaint_agent" in result["agents_used"]
        assert "product_agent" in result["agents_used"]

    @pytest.mark.asyncio
    async def test_missing_subtask_agent_skipped(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = HierarchicalMode(bus=bus, bb=bb)
        agents = {"general_agent": _mock_agent("汇总")}

        result = await mode.execute(
            agents, _state("投诉"),
            {
                "coordinator": "general_agent",
                "sub_tasks": {"missing_agent": "任务"},
            },
        )

        assert result["response"] == "汇总"

    @pytest.mark.asyncio
    async def test_timeout_proceeds_with_coordinator(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = HierarchicalMode(bus=bus, bb=bb)
        mode._HIERARCHICAL_TIMEOUT = 0.001

        async def _slow(state):
            await asyncio.sleep(10)
            return {"response": "slow"}

        slow = AsyncMock()
        slow.process_with_retry = _slow
        coordinator = _mock_agent("协调汇总")

        agents = {"general_agent": coordinator, "slow_agent": slow}

        result = await mode.execute(
            agents, _state("问题"),
            {
                "coordinator": "general_agent",
                "sub_tasks": {"slow_agent": "慢任务"},
            },
        )

        assert result["response"] == "协调汇总"

    @pytest.mark.asyncio
    async def test_blackboard_subtask_data_merged(self):
        bus, bb = _mock_bus(), _mock_bb()
        bb.read_prefix = AsyncMock(
            return_value={"hierarchical.subtask.extra_agent": {"response": "BB 数据"}}
        )
        mode = HierarchicalMode(bus=bus, bb=bb)
        agents = {"general_agent": _mock_agent("汇总")}

        result = await mode.execute(
            agents, _state("问题"),
            {"coordinator": "general_agent", "sub_tasks": {}},
        )

        assert result["response"] == "汇总"

    @pytest.mark.asyncio
    async def test_empty_sub_tasks(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = HierarchicalMode(bus=bus, bb=bb)
        agents = {"general_agent": _mock_agent("直接汇总")}

        result = await mode.execute(
            agents, _state(), {"coordinator": "general_agent", "sub_tasks": {}}
        )

        assert result["response"] == "直接汇总"


# ── ReActMode ────────────────────────────────────────────────────


class TestReActMode:

    @pytest.mark.asyncio
    async def test_react_agent_found(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ReActMode(bus=bus, bb=bb)
        agents = {"react_agent": _mock_agent("推理结果")}

        result = await mode.execute(agents, _state(), {"primary_agent": "general_agent"})

        assert result["response"] == "推理结果"
        assert result["mode"] == "react"
        assert result["agents_used"] == ["react_agent"]

    @pytest.mark.asyncio
    async def test_react_agent_not_found_fallback_to_primary(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ReActMode(bus=bus, bb=bb)
        agents = {"general_agent": _mock_agent("回退结果")}

        result = await mode.execute(agents, _state(), {"primary_agent": "general_agent"})

        assert result["response"] == "回退结果"
        assert result["agents_used"] == ["general_agent"]

    @pytest.mark.asyncio
    async def test_no_agent_available(self):
        mode = ReActMode()
        result = await mode.execute({}, _state(), {"primary_agent": "missing"})

        assert "无可用 Agent" in result["response"]
        assert result["mode"] == "react"
        assert result["agents_used"] == []

    @pytest.mark.asyncio
    async def test_publishes_events(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = ReActMode(bus=bus, bb=bb)
        agents = {"react_agent": _mock_agent("result")}

        await mode.execute(agents, _state(), {})

        assert bus.publish.call_count == 2  # start + complete


# ── CollaborationMode 基类辅助方法 ────────────────────────────────


class TestCollaborationModeHelpers:

    @pytest.mark.asyncio
    async def test_safe_publish_with_bus(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = SequentialMode(bus=bus, bb=bb)

        await mode._safe_publish("test.topic", "sender", {"key": "value"})

        bus.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_safe_publish_without_bus(self):
        mode = SequentialMode(bus=None, bb=None)
        await mode._safe_publish("test.topic", "sender", {})  # 不应报错

    @pytest.mark.asyncio
    async def test_safe_publish_exception_swallowed(self):
        bus = AsyncMock()
        bus.publish = AsyncMock(side_effect=RuntimeError("bus broken"))
        mode = SequentialMode(bus=bus, bb=None)

        await mode._safe_publish("test.topic", "sender", {})  # 不应抛出

    @pytest.mark.asyncio
    async def test_safe_bb_write_with_bb(self):
        bus, bb = _mock_bus(), _mock_bb()
        mode = SequentialMode(bus=bus, bb=bb)

        await mode._safe_bb_write("key", {"data": 1}, ttl=60.0)

        bb.write.assert_called_once_with("key", {"data": 1}, ttl=60.0)

    @pytest.mark.asyncio
    async def test_safe_bb_write_without_bb(self):
        mode = SequentialMode(bus=None, bb=None)
        await mode._safe_bb_write("key", "val")  # 不应报错

    @pytest.mark.asyncio
    async def test_safe_bb_write_exception_swallowed(self):
        bb = AsyncMock()
        bb.write = AsyncMock(side_effect=RuntimeError("bb broken"))
        mode = SequentialMode(bus=None, bb=bb)

        await mode._safe_bb_write("key", "val")  # 不应抛出

    @pytest.mark.asyncio
    async def test_safe_bb_read_prefix_with_bb(self):
        bb = _mock_bb()
        bb.read_prefix = AsyncMock(return_value={"k": "v"})
        mode = SequentialMode(bus=None, bb=bb)

        result = await mode._safe_bb_read_prefix("k")

        assert result == {"k": "v"}

    @pytest.mark.asyncio
    async def test_safe_bb_read_prefix_without_bb(self):
        mode = SequentialMode(bus=None, bb=None)
        result = await mode._safe_bb_read_prefix("k")
        assert result == {}

    @pytest.mark.asyncio
    async def test_safe_bb_read_prefix_exception_returns_empty(self):
        bb = AsyncMock()
        bb.read_prefix = AsyncMock(side_effect=RuntimeError("bb broken"))
        mode = SequentialMode(bus=None, bb=bb)

        result = await mode._safe_bb_read_prefix("k")
        assert result == {}
