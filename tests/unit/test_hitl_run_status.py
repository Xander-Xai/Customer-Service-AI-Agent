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
    APPROVAL_RESUMABLE_STATUSES,
    EXECUTABLE_STATUSES,
    TERMINAL_STATUSES,
    InvalidRunTransition,
    RunStatus,
    can_transition,
    ensure_transition,
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
