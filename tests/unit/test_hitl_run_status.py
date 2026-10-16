"""AgentRun 状态机中的 ``WAITING_APPROVAL`` 契约测试。

审批等待是一个**新的非终态**，它的设计错误会直接变成线上事故：

============================  ==========================================
不变量                        违反后的后果
============================  ==========================================
不进 EXECUTABLE_STATUSES      通用队列轮询把没人处理的审批反复捞起（忙循环）
不进 TERMINAL_STATUSES        审批中的 run 被当成已完成，副作用永不到达
WAITING_APPROVAL -> RUNNING   审批后无法恢复
保留 -> DEAD_LETTER/CANCELLED 审批被永久搁置时 run 无处可去
============================  ==========================================
"""

from __future__ import annotations

import pytest

from runtime.statuses import (
    ADMINISTRATIVE_TRANSITIONS,
    APPROVAL_RESUMABLE_STATUSES,
    EXECUTABLE_STATUSES,
    TERMINAL_STATUSES,
    InvalidRunTransition,
    RunStatus,
    can_transition,
    ensure_declared_transition,
    ensure_transition,
    is_declared_transition,
    is_terminal,
    parse_status,
)


class TestRegistration:
    def test_enum_member_exists(self):
        assert RunStatus.WAITING_APPROVAL.value == "WAITING_APPROVAL"
        assert parse_status("waiting_approval") is RunStatus.WAITING_APPROVAL

    def test_is_non_terminal(self):
        """审批中的 run 不能被当成完成——副作用还没发生。"""
        assert is_terminal(RunStatus.WAITING_APPROVAL) is False
        assert RunStatus.WAITING_APPROVAL not in TERMINAL_STATUSES

    def test_not_executable_by_generic_polling(self):
        """关键取舍：通用队列轮询**不得**捞起等待审批的 run。

        否则没人处理的审批会变成忙循环，持续占用 worker 并刷 checkpoint。
        只有审批 API 显式 dispatch 才能恢复它。
        """
        assert RunStatus.WAITING_APPROVAL not in EXECUTABLE_STATUSES
        assert RunStatus.WAITING_APPROVAL in APPROVAL_RESUMABLE_STATUSES

    def test_every_status_has_a_transition_entry(self):
        for status in RunStatus:
            assert status in can_transition.__globals__["ALLOWED_TRANSITIONS"], status


class TestAdministrativeTransitions:
    """运维性 / 非进展性迁移必须**显式声明**，而不是绕过状态表。

    这些边都是真实执行的（人工重放、thread 竞争退避、RUNNING 接管）。它们此前
    直接调用 ``_transition`` 而不经过 ``ensure_transition``，使
    ``ALLOWED_TRANSITIONS`` 作为「唯一真相源」的声明与实际行为不符。
    """

    def test_redrive_edge_is_declared(self):
        assert RunStatus.QUEUED in ADMINISTRATIVE_TRANSITIONS[RunStatus.DEAD_LETTER]
        assert is_declared_transition(RunStatus.DEAD_LETTER, RunStatus.QUEUED)

    @pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.RETRYING, RunStatus.RUNNING])
    def test_self_edges_are_declared(self, status):
        assert is_declared_transition(status, status)

    def test_admin_table_does_not_weaken_the_terminal_contract(self):
        """「终态没有自动出口」必须继续成立且可测。

        ``DEAD_LETTER`` 的「终态」含义是「没有任何自动路径会再捞起它」，
        不是「人永远不能重放」——重放是显式人工动作。
        """
        for terminal in TERMINAL_STATUSES:
            for target in RunStatus:
                if target in ADMINISTRATIVE_TRANSITIONS.get(terminal, frozenset()):
                    # 唯一的例外就是显式重放，且只有 DEAD_LETTER 有
                    assert (terminal, target) == (RunStatus.DEAD_LETTER, RunStatus.QUEUED)
                    continue
                assert can_transition(terminal, target) is False, (terminal, target)

    def test_undeclared_edges_are_rejected(self):
        with pytest.raises(InvalidRunTransition):
            ensure_declared_transition(RunStatus.SUCCEEDED, RunStatus.QUEUED)
        with pytest.raises(InvalidRunTransition):
            ensure_declared_transition(RunStatus.DEAD_LETTER, RunStatus.RUNNING)


class TestTransitions:
    def test_running_can_park_for_approval(self):
        assert can_transition(RunStatus.RUNNING, RunStatus.WAITING_APPROVAL) is True

    def test_approval_can_resume(self):
        assert can_transition(RunStatus.WAITING_APPROVAL, RunStatus.RUNNING) is True

    @pytest.mark.parametrize("target", [RunStatus.DEAD_LETTER, RunStatus.CANCELLED])
    def test_escape_hatches_exist(self, target):
        """审批被永久搁置时 run 必须有出口。"""
        assert can_transition(RunStatus.WAITING_APPROVAL, target) is True

    @pytest.mark.parametrize(
        "target",
        [
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.QUEUED,
            RunStatus.PENDING,
            RunStatus.RETRYING,
        ],
    )
    def test_forbidden_targets(self, target):
        assert can_transition(RunStatus.WAITING_APPROVAL, target) is False

    def test_no_queued_edge_on_purpose(self):
        """刻意没有 WAITING_APPROVAL -> QUEUED。

        若审批 API 把 run 改回 QUEUED，worker 领取会走 ``mark_running`` 并把
        attempt +1；``AGENT_RUN_MAX_ATTEMPTS`` 默认 3，于是「等人审批」会被当成
        三次失败重试，审批还没落地 run 就已 DEAD_LETTER。等待人不是失败。
        """
        assert can_transition(RunStatus.WAITING_APPROVAL, RunStatus.QUEUED) is False

    def test_ensure_transition_raises_on_illegal(self):
        with pytest.raises(InvalidRunTransition):
            ensure_transition(RunStatus.WAITING_APPROVAL, RunStatus.SUCCEEDED, "run-1")

    def test_ensure_transition_accepts_legal(self):
        ensure_transition(RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, "run-1")
        ensure_transition(RunStatus.WAITING_APPROVAL, RunStatus.RUNNING, "run-1")

    def test_terminal_states_have_no_exit(self):
        for terminal in TERMINAL_STATUSES:
            for target in RunStatus:
                assert can_transition(terminal, target) is False, (terminal, target)

    def test_existing_states_unaffected(self):
        """回归：原有迁移语义不变。"""
        assert can_transition(RunStatus.QUEUED, RunStatus.RUNNING) is True
        assert can_transition(RunStatus.RUNNING, RunStatus.RETRYING) is True
        assert can_transition(RunStatus.RETRYING, RunStatus.RUNNING) is True
        assert can_transition(RunStatus.RUNNING, RunStatus.SUCCEEDED) is True
        assert RunStatus.QUEUED in EXECUTABLE_STATUSES
        assert RunStatus.RETRYING in EXECUTABLE_STATUSES
