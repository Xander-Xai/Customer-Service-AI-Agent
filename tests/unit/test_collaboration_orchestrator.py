"""
测试 collaboration/orchestrator.py — 协作编排器覆盖率补齐
覆盖：_select_mode 全分支、upgrade_mode 全分支、select_mode_name、build_context
"""

from unittest.mock import MagicMock, patch

from collaboration.orchestrator import CollaborationOrchestrator, _has_keywords
from core.message_bus import MessageBus
from core.shared_blackboard import SharedBlackboard


def _mock_bus():
    return MagicMock(spec=MessageBus)


def _mock_bb():
    return MagicMock(spec=SharedBlackboard)


def _routing(query_type="product_info", complexity=10, fast_path=False, agent_name="product_agent"):
    """创建 mock routing_result"""
    r = MagicMock()
    r.query_type = query_type
    r.complexity = complexity
    r.fast_path = fast_path
    r.agent_name = agent_name
    return r


def _state(query=""):
    return {"customer_query": query, "session_id": "test"}


class TestHasKeywords:
    def test_match_found(self):
        assert _has_keywords("这个产品效果怎么样", {"产品", "效果"}) is True

    def test_no_match(self):
        assert _has_keywords("今天天气好", {"产品", "效果"}) is False

    def test_empty_keywords(self):
        assert _has_keywords("任何查询", set()) is False


class TestSelectMode:
    def setup_method(self):
        self.orch = CollaborationOrchestrator(_mock_bus(), _mock_bb())

    def test_fast_path_returns_sequential(self):
        routing = _routing(fast_path=True, agent_name="product_agent")
        mode, ctx = self.orch._select_mode(routing, _state("你好"))
        assert mode == "sequential"
        assert ctx["primary_agent"] == "product_agent"

    def test_complaint_returns_hierarchical(self):
        routing = _routing(query_type="complaint", agent_name="complaint_agent")
        mode, ctx = self.orch._select_mode(routing, _state("产品质量问题投诉"))
        assert mode == "hierarchical"
        assert ctx["coordinator"] == "general_agent"
        assert "complaint_agent" in ctx["sub_tasks"]

    def test_complaint_with_product_keywords(self):
        """投诉查询包含产品关键词时，product_agent 加入子任务"""
        routing = _routing(query_type="complaint")
        mode, ctx = self.orch._select_mode(routing, _state("产品有问题要投诉"))
        assert mode == "hierarchical"
        assert "product_agent" in ctx["sub_tasks"]

    def test_complaint_with_billing_keywords(self):
        """投诉查询包含退款/订单关键词时，billing_agent 加入子任务"""
        routing = _routing(query_type="complaint")
        mode, ctx = self.orch._select_mode(routing, _state("退款投诉订单有问题"))
        assert mode == "hierarchical"
        assert "billing_agent" in ctx["sub_tasks"]

    def test_high_complexity_multi_domain_returns_react(self):
        """高复杂度(≥60) + 多领域关键词 → ReAct 模式"""
        routing = _routing(complexity=80, agent_name="general_agent")
        mode, ctx = self.orch._select_mode(routing, _state("产品过敏退款怎么办"))
        assert mode == "react"

    def test_multi_domain_returns_parallel(self):
        """中等复杂度 + 2+领域关键词 → parallel"""
        routing = _routing(complexity=30, agent_name="product_agent")
        mode, ctx = self.orch._select_mode(routing, _state("产品和退款问题"))
        assert mode == "parallel"

    def test_consultation_for_product_agent(self):
        """product_agent + 高复杂度 → 咨询 tech_agent"""
        routing = _routing(complexity=70, agent_name="product_agent")
        mode, ctx = self.orch._select_mode(routing, _state("产品效果"))
        assert mode == "consultation"
        assert "tech_agent" in ctx["consult_agents"]

    def test_consultation_for_billing_agent(self):
        """billing_agent + 高复杂度 → 咨询 product_agent"""
        routing = _routing(complexity=70, agent_name="billing_agent")
        mode, ctx = self.orch._select_mode(routing, _state("退款"))
        assert mode == "consultation"
        assert "product_agent" in ctx["consult_agents"]

    def test_consultation_for_tech_agent(self):
        """tech_agent + 高复杂度 → 咨询 product_agent"""
        routing = _routing(complexity=70, agent_name="tech_agent")
        mode, ctx = self.orch._select_mode(routing, _state("技术"))
        assert mode == "consultation"
        assert "product_agent" in ctx["consult_agents"]

    def test_default_sequential(self):
        """低复杂度 + 无多领域关键词 → sequential"""
        routing = _routing(complexity=10, agent_name="general_agent")
        mode, ctx = self.orch._select_mode(routing, _state("一般问题"))
        assert mode == "sequential"
        assert ctx["primary_agent"] == "general_agent"

    def test_consultation_low_complexity_returns_sequential(self):
        """低复杂度 + 无多领域关键词 → 不触发 consultation"""
        routing = _routing(complexity=10, agent_name="product_agent")
        mode, ctx = self.orch._select_mode(routing, _state("一般问题"))
        assert mode == "sequential"


class TestSelectModeName:
    def setup_method(self):
        self.orch = CollaborationOrchestrator(_mock_bus(), _mock_bb())

    def test_returns_mode_name_string(self):
        routing = _routing(fast_path=True)
        name = self.orch.select_mode_name(routing, _state())
        assert name == "sequential"


class TestBuildContext:
    def setup_method(self):
        self.orch = CollaborationOrchestrator(_mock_bus(), _mock_bb())

    def test_returns_tuple(self):
        routing = _routing(fast_path=True, agent_name="tech_agent")
        mode, ctx = self.orch.build_context(routing, _state())
        assert mode == "sequential"
        assert ctx["primary_agent"] == "tech_agent"


class TestUpgradeMode:
    def setup_method(self):
        self.orch = CollaborationOrchestrator(_mock_bus(), _mock_bb())

    def test_sequential_to_consultation(self):
        with patch("collaboration.orchestrator.MODE_UPGRADE_ENABLED", True):
            mode, ctx = self.orch.upgrade_mode("sequential", {"current_agent": "product_agent"})
        assert mode == "consultation"
        assert "consult_agents" in ctx

    def test_consultation_to_parallel(self):
        with patch("collaboration.orchestrator.MODE_UPGRADE_ENABLED", True):
            mode, ctx = self.orch.upgrade_mode("consultation", {"current_agent": "tech_agent"})
        assert mode == "parallel"
        assert "agent_list" in ctx

    def test_parallel_to_react(self):
        with patch("collaboration.orchestrator.MODE_UPGRADE_ENABLED", True):
            mode, ctx = self.orch.upgrade_mode("parallel", {"current_agent": "billing_agent"})
        assert mode == "react"
        assert ctx["primary_agent"] == "billing_agent"

    def test_react_is_terminal_returns_same(self):
        with patch("collaboration.orchestrator.MODE_UPGRADE_ENABLED", True):
            mode, ctx = self.orch.upgrade_mode("react", {"current_agent": "general_agent"})
        assert mode == "react"

    def test_disabled_returns_same_mode(self):
        with patch("collaboration.orchestrator.MODE_UPGRADE_ENABLED", False):
            mode, ctx = self.orch.upgrade_mode("sequential", {"current_agent": "product_agent"})
        assert mode == "sequential"
        assert ctx == {}

    def test_parallel_agent_list_capped_at_3(self):
        with patch("collaboration.orchestrator.MODE_UPGRADE_ENABLED", True):
            mode, ctx = self.orch.upgrade_mode("consultation", {"current_agent": "product_agent"})
        assert mode == "parallel"
        assert len(ctx["agent_list"]) <= 3

    def test_upgrade_consultation_default_agent(self):
        """当前 agent 不在 consult_map 时，回退到 product_agent"""
        with patch("collaboration.orchestrator.MODE_UPGRADE_ENABLED", True):
            mode, ctx = self.orch.upgrade_mode("sequential", {"current_agent": "unknown_agent"})
        assert mode == "consultation"
        assert "product_agent" in ctx["consult_agents"]

    def test_upgrade_parallel_excludes_self(self):
        """并行升级时，agent_list 包含自身（第一个）"""
        with patch("collaboration.orchestrator.MODE_UPGRADE_ENABLED", True):
            mode, ctx = self.orch.upgrade_mode("consultation", {"current_agent": "product_agent"})
        assert mode == "parallel"
        assert "product_agent" in ctx["agent_list"]

    def test_upgrade_from_unknown_mode_returns_same(self):
        """未知模式 → 无升级路径"""
        with patch("collaboration.orchestrator.MODE_UPGRADE_ENABLED", True):
            mode, ctx = self.orch.upgrade_mode("unknown_mode", {"current_agent": "general_agent"})
        assert mode == "unknown_mode"
