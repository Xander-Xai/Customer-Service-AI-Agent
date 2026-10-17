"""AgentRun 状态机定义。

业务状态真相源是数据库 ``agent_runs`` 表；本模块只定义合法迁移，
由 ``runtime/run_service.py`` 在数据库层强制（原子条件更新，防并发竞态）。

状态生命周期::

    PENDING ──► QUEUED ──► RUNNING ──┬──► SUCCEEDED        (终态)
                          │           ├──► FAILED           (终态，permanent error，不重试)
                          │           ├──► RETRYING ──► RUNNING (transient error，退避后重试)
                          │           ├──► WAITING_APPROVAL ──► RUNNING (人工审批，见下)
                          │           └──► DEAD_LETTER      (终态，retry 用尽)
                          └──► CANCELLED                 (终态，用户/管理员取消)

约束：
  - ``SUCCEEDED`` / ``FAILED`` / ``DEAD_LETTER`` / ``CANCELLED`` 为终态；
  - 不允许 ``SUCCEEDED -> RUNNING`` 等回退；
  - retry 走显式 ``RUNNING -> RETRYING -> RUNNING``，并递增 attempt。

``PENDING`` 表示「已落库、尚未投递到队列」。默认创建路径直接写 ``QUEUED``
（保持既有 ``POST /api/runs`` 语义不变）；需要先落库再投递的两阶段流程可用
``create_run(status=PENDING)`` + ``mark_queued()``。

``WAITING_APPROVAL``（human-in-the-loop，非终态）
------------------------------------------------
高风险工具副作用被拦下等人工决策时，图在 ``interrupt()`` 处挂起、checkpoint
落库，run 转入本状态。它**不是**终态，也**不是**失败：等待人不是重试预算的一部分。

关键取舍：
  - **不进** ``EXECUTABLE_STATUSES``：只有审批 API 显式投递才会恢复，通用队列
    轮询不会把没人处理的审批当成待办反复捞起。
  - ``WAITING_APPROVAL -> RUNNING`` 由 ``RunService.mark_resumed_running()``
    执行，**不递增 attempt**：人等了 3 小时不该消耗 3 次重试（默认
    ``AGENT_RUN_MAX_ATTEMPTS=3``，若复用 RETRYING 轮询会在审批落地前就
    DEAD_LETTER）。
  - 保留 ``WAITING_APPROVAL -> DEAD_LETTER`` / ``CANCELLED`` 逃生口，避免
    审批被永久搁置时 run 无处可去。
"""

from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
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

#: 可由「人工审批已决策」恢复执行的状态。
#:
#: 与 ``EXECUTABLE_STATUSES`` 刻意分开：等待审批的 run 只能被审批 API 显式
#: 投递恢复，通用队列轮询不得把它捞起（否则无人处理的审批会变成忙循环）。
APPROVAL_RESUMABLE_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.WAITING_APPROVAL})

#: 允许迁移
ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED, RunStatus.DEAD_LETTER}),
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
            RunStatus.WAITING_APPROVAL,
            RunStatus.DEAD_LETTER,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.RETRYING: frozenset({RunStatus.RUNNING, RunStatus.DEAD_LETTER, RunStatus.CANCELLED}),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.DEAD_LETTER, RunStatus.CANCELLED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.DEAD_LETTER: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

#: 显式声明的**非进展性 / 运维性**迁移，与 ``ALLOWED_TRANSITIONS`` 分开。
#:
#: 这些边都是真实执行的（此前它们绕过了 ``ensure_transition``，导致本表作为
#: 「唯一真相源」的声明与实际行为不符）：
#:
#: - ``DEAD_LETTER -> QUEUED``：人工重放（``requeue_dead_letter`` /
#:   ``scripts/replay_dead_run.py``）。**没有任何自动路径**会做这件事。
#: - ``QUEUED -> QUEUED`` / ``RETRYING -> RETRYING`` / ``RUNNING -> RUNNING``：
#:   不改变业务阶段的推进——thread 竞争退避（``defer_run``）与 RUNNING 接管
#:   （``takeover_running``，在同一条 UPDATE 里 attempt+1）。
#:
#: 保持独立而不是并进 ``ALLOWED_TRANSITIONS``，是为了让「终态没有自动出口」这句话
#: 继续成立且可测（见 ``tests/unit/test_hitl_run_status.py``）；``DEAD_LETTER`` 的
#: 「终态」含义是「没有任何东西会自己再捞起它」，不是「人永远不能重放」。
ADMINISTRATIVE_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.DEAD_LETTER: frozenset({RunStatus.QUEUED}),
    RunStatus.QUEUED: frozenset({RunStatus.QUEUED}),
    RunStatus.RETRYING: frozenset({RunStatus.RETRYING}),
    RunStatus.RUNNING: frozenset({RunStatus.RUNNING}),
}


def is_declared_transition(src: RunStatus, dst: RunStatus) -> bool:
    """``ALLOWED_TRANSITIONS`` 与 ``ADMINISTRATIVE_TRANSITIONS`` 的并集。"""
    return dst in ALLOWED_TRANSITIONS.get(
        src, frozenset()
    ) or dst in ADMINISTRATIVE_TRANSITIONS.get(src, frozenset())


def ensure_declared_transition(src: RunStatus, dst: RunStatus, run_id: str = "") -> None:
    """未在任一表中声明的迁移直接拒绝——表才是真相源，而不是文档。"""
    if not is_declared_transition(src, dst):
        raise InvalidRunTransition(run_id, src.value, dst.value)


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
