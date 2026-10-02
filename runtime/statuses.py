"""AgentRun 状态机定义。

业务状态真相源是数据库 ``agent_runs`` 表；本模块只定义合法迁移，
由 ``runtime/run_service.py`` 在数据库层强制（原子条件更新，防并发竞态）。

状态生命周期::

    PENDING ──► QUEUED ──► RUNNING ──┬──► SUCCEEDED        (终态)
                          │           ├──► FAILED           (终态，permanent error，不重试)
                          │           ├──► RETRYING ──► RUNNING (transient error，退避后重试)
                          │           └──► DEAD_LETTER      (终态，retry 用尽)
                          └──► CANCELLED                 (终态，用户/管理员取消)

约束：
  - ``SUCCEEDED`` / ``FAILED`` / ``DEAD_LETTER`` / ``CANCELLED`` 为终态；
  - 不允许 ``SUCCEEDED -> RUNNING`` 等回退；
  - retry 走显式 ``RUNNING -> RETRYING -> RUNNING``，并递增 attempt。

``PENDING`` 表示「已落库、尚未投递到队列」。默认创建路径直接写 ``QUEUED``
（保持既有 ``POST /api/runs`` 语义不变）；需要先落库再投递的两阶段流程可用
``create_run(status=PENDING)`` + ``mark_queued()``。
"""

from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.DEAD_LETTER,
        RunStatus.CANCELLED,
    }
)

#: 可被 worker 领取执行的状态
EXECUTABLE_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.QUEUED, RunStatus.RETRYING})

#: 允许迁移
ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset(
        {RunStatus.QUEUED, RunStatus.CANCELLED, RunStatus.DEAD_LETTER}
    ),
    RunStatus.QUEUED: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.DEAD_LETTER,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.RETRYING,
            RunStatus.DEAD_LETTER,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.RETRYING: frozenset(
        {RunStatus.RUNNING, RunStatus.DEAD_LETTER, RunStatus.CANCELLED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.DEAD_LETTER: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class InvalidRunTransition(ValueError):
    """非法状态迁移。"""

    def __init__(self, run_id: str, current: str, target: str):
        self.run_id = run_id
        self.current = current
        self.target = target
        super().__init__(f"非法状态迁移: run={run_id} {current} -> {target}")


def parse_status(value: RunStatus | str) -> RunStatus:
    """把字符串/枚举解析为 RunStatus，未知值抛 ValueError。"""
    if isinstance(value, RunStatus):
        return value
    try:
        return RunStatus(str(value).upper())
    except ValueError as e:
        raise ValueError(f"未知 AgentRun 状态: {value!r}") from e


def can_transition(current: RunStatus | str, target: RunStatus | str) -> bool:
    cur = parse_status(current)
    tgt = parse_status(target)
    return tgt in ALLOWED_TRANSITIONS[cur]


def ensure_transition(current: RunStatus | str, target: RunStatus | str, run_id: str = "") -> None:
    """校验迁移，非法时抛 InvalidRunTransition。"""
    cur = parse_status(current)
    tgt = parse_status(target)
    if tgt not in ALLOWED_TRANSITIONS[cur]:
        raise InvalidRunTransition(run_id, cur.value, tgt.value)


def is_terminal(value: RunStatus | str) -> bool:
    return parse_status(value) in TERMINAL_STATUSES
