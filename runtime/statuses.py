"""AgentRun 状态机定义。

业务状态真相源是 PostgreSQL 的 ``agent_runs`` 表；本模块只定义合法迁移，
由 ``runtime/run_service.py`` 在数据库层强制（条件更新，防并发竞态）。

关键约束：
  - ``SUCCEEDED`` / ``CANCELLED`` / ``DEAD`` 为终态；
  - 不允许 ``SUCCEEDED -> RUNNING``、``FAILED -> RUNNING``；
    retry 走显式 ``FAILED -> QUEUED``（并递增 attempt）。
"""

from enum import Enum


class RunStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DEAD = "DEAD"


class InvalidRunTransition(ValueError):
    """非法状态迁移。"""

    def __init__(self, run_id: str, current: str, target: str):
        self.run_id = run_id
        self.current = current
        self.target = target
        super().__init__(f"非法状态迁移: run={run_id} {current} -> {target}")


TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.CANCELLED, RunStatus.DEAD}
)

#: 允许迁移（显式 retry = FAILED -> QUEUED，且必须递增 attempt）
ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED, RunStatus.DEAD}),
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.DEAD}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.DEAD,
            RunStatus.WAITING_APPROVAL,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.QUEUED, RunStatus.CANCELLED, RunStatus.DEAD}
    ),
    RunStatus.FAILED: frozenset({RunStatus.QUEUED, RunStatus.DEAD}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.DEAD: frozenset(),
}

#: 可在开始执行前取消的状态
CANCELLABLE_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.PENDING, RunStatus.QUEUED}
)


def parse_status(value: "RunStatus | str") -> RunStatus:
    """把字符串/枚举解析为 RunStatus，未知值抛 ValueError。"""
    if isinstance(value, RunStatus):
        return value
    try:
        return RunStatus(str(value).upper())
    except ValueError as e:
        raise ValueError(f"未知 AgentRun 状态: {value!r}") from e


def can_transition(current: "RunStatus | str", target: "RunStatus | str") -> bool:
    cur = parse_status(current)
    tgt = parse_status(target)
    return tgt in ALLOWED_TRANSITIONS[cur]


def ensure_transition(current: "RunStatus | str", target: "RunStatus | str", run_id: str = "") -> None:
    """校验迁移，非法时抛 InvalidRunTransition。"""
    cur = parse_status(current)
    tgt = parse_status(target)
    if tgt not in ALLOWED_TRANSITIONS[cur]:
        raise InvalidRunTransition(run_id, cur.value, tgt.value)
