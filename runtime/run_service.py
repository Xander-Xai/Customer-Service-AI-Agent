"""AgentRun 服务层：所有状态迁移的唯一入口。

路由/worker 不得直接改数据库状态，必须经此服务，从而保证：
  - 合法迁移（``runtime/statuses.py``）；
  - 原子条件更新（并发 worker / 重复投递不会互相覆盖）；
  - 时间戳、错误分类、retry 调度一致。

可靠性语义：
  - ``attempt`` 在执行开始时（``mark_running``）递增，表示已开始的执行次数；
  - ``RUNNING`` 携带 worker_id + task_id + lease（ownership），lease 过期可被
    其他 worker 接管（崩溃恢复）；
  - transient error 走 ``RUNNING -> RETRYING`` 并写 ``next_retry_at``；
  - retry 用尽 / permanent error 写 ``DEAD_LETTER`` / ``FAILED``。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from core.logger import get_logger

from .repository import AgentRunRepository
from .statuses import (
    EXECUTABLE_STATUSES,
    TERMINAL_STATUSES,
    InvalidRunTransition,
    RunStatus,
    ensure_declared_transition,
    ensure_transition,
    parse_status,
)

logger = get_logger("runtime.service")


class RunNotFound(LookupError):
    """指定 run_id 不存在。"""


class RunOwnershipLost(RuntimeError):
    """worker 已不再拥有该 run：提交被 owner CAS 拒绝。

    必须与 :class:`InvalidRunTransition` 区分。两者都是「写不进去」，但含义
    完全不同：

    - ``InvalidRunTransition``：状态机不接受这次迁移（例如 run 已 SUCCEEDED）。
    - ``RunOwnershipLost``：状态还是 RUNNING，但 ``worker_id`` 已经不是我了，
      或者我的 lease 已经过期。**这不是业务失败**。

    混淆两者会导致严重后果：一个失去所有权的 worker 会把「我不是 owner 了」
    当成 permanent error 去 ``mark_failed``，从而覆盖新 owner 正在跑的 run。
    所以这里刻意带 ``current_status``，让调用方能区分「状态不对」与
    「owner 不对」。

    只携带安全字段：run_id / 期望的 owner / 当前状态 / 当前 owner。不含 query、
    result、tool args 或任何用户数据。
    """

    def __init__(
        self,
        run_id: str,
        *,
        expected_worker_id: str | None,
        current_status: str | None = None,
        current_worker_id: str | None = None,
    ):
        self.run_id = run_id
        self.expected_worker_id = expected_worker_id
        self.current_status = current_status
        self.current_worker_id = current_worker_id
        super().__init__(
            f"run 所有权已丢失: run={run_id} "
            f"expected_worker={expected_worker_id} "
            f"current_status={current_status} current_worker={current_worker_id}"
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime | None) -> datetime | None:
    """SQLite 可能返回 naive datetime；统一为 UTC aware 以便比较。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _default_max_attempts() -> int:
    from core.config import AGENT_RUN_MAX_ATTEMPTS

    return AGENT_RUN_MAX_ATTEMPTS


#: ``agent_runs.idempotency_key`` 列宽（见 db/models.py / alembic 004）。
IDEMPOTENCY_KEY_MAX_LENGTH = 128


def build_idempotency_scope(user_id: str | None, endpoint: str, raw_key: str) -> str:
    """构造幂等作用域：user + endpoint + idempotency_key。

    不同用户/不同端点可复用同一个原始 key；同作用域内 DB 唯一约束保证只创建一个 run。

    作用域串长度不受调用方控制（``Idempotency-Key`` 请求头无长度上限），拼接后可能
    超过列宽 ``String(128)`` 并在 PostgreSQL 触发 ``value too long``。因此对拼接结果
    取 sha256，输出固定 64 字符：仍然确定性、仍然按 user+endpoint 分域，且永不溢出。
    """
    scope = f"{(user_id or 'anon')}:{endpoint}:{raw_key}"
    if len(scope) <= IDEMPOTENCY_KEY_MAX_LENGTH:
        return scope
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()


class RunService:
    def __init__(self, repository: AgentRunRepository | None = None):
        self.repo = repository or AgentRunRepository()

    # ---- 创建 / 查询 ----

    def create_run(
        self,
        *,
        query: str,
        session_id: str,
        user_id: str | None = None,
        thread_id: str | None = None,
        max_attempts: int | None = None,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
        status: str = RunStatus.QUEUED.value,
    ) -> dict[str, Any]:
        """创建 run；提供 idempotency_key（已作用域化）时命中已有 run 直接返回。

        默认状态 ``QUEUED``（保持既有 ``POST /api/runs`` 语义）；需要「先落库、后
        投递」的两阶段流程时传 ``status=RunStatus.PENDING``，投递成功后再
        ``mark_queued()``。
        """
        if idempotency_key:
            existing = self.repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
        return self.repo.create(
            run_id=str(uuid.uuid4()),
            thread_id=thread_id or session_id,
            session_id=session_id,
            query=query,
            user_id=user_id,
            max_attempts=(max_attempts if max_attempts is not None else _default_max_attempts()),
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            status=status,
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.repo.get(run_id)

    def get_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        return self.repo.get_by_idempotency_key(key)

    def require_run(self, run_id: str) -> dict[str, Any]:
        run = self.repo.get(run_id)
        if run is None:
            raise RunNotFound(run_id)
        return run

    def list_recoverable_runs(self, *, limit: int = 100, now: datetime | None = None) -> list[str]:
        """列出"卡住"的 run：RETRYING/QUEUED 且 next_retry_at 已到。

        用于重试投递失败后的兜底恢复（见 ``runtime/retry.py::reconcile_stuck_runs``）。

        刻意**不含**过期 lease 的 RUNNING（由 :meth:`list_stale_running_runs`
        覆盖），也**不含** PENDING（由 :meth:`list_stale_pending_runs` 覆盖：PENDING
        需要先做条件迁移再投递，不是无脑重发）。
        """
        return self.repo.list_recoverable_runs(limit=limit, now=now or _utcnow())

    def list_stale_pending_runs(
        self, *, limit: int = 100, now: datetime | None = None, grace_seconds: int = 60
    ) -> list[str]:
        """列出落库超过宽限期、仍停在 ``PENDING`` 的 run（两阶段入队恢复）。

        见 :meth:`promote_pending_to_queued` 的契约说明。刻意**不**扫描全部
        PENDING：投递结果未知的 run 需要一个宽限期才允许补投。
        """
        return self.repo.list_stale_pending_runs(
            limit=limit, now=now or _utcnow(), grace_seconds=grace_seconds
        )

    def list_resumable_waiting_approval_runs(self, *, limit: int = 100) -> list[str]:
        """列出「决策已落定但未恢复」的 WAITING_APPROVAL run（Issue #14）。

        见 :meth:`AgentRunRepository.list_resumable_waiting_approval_runs`。由
        reconciler 重新投递；状态迁移与幂等由 worker 侧 CAS + ledger 负责。
        """
        return self.repo.list_resumable_waiting_approval_runs(limit=limit)

    def promote_pending_to_queued(
        self, run_id: str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """条件迁移 ``PENDING -> QUEUED``，成功后调用方才可投递。

        为什么是「先迁移、再发消息」而不是「直接发消息」
        -------------------------------------------
        直接对 PENDING 重投无法区分两种情况：（a）进程在 publish **之前**被打断
        —— 必须补投；（b）publish 成功但 ``mark_queued`` 未提交 —— 队列里已经有
        一条消息，补投是多余投递。区分不了就只能无脑重投。

        先原子地把 PENDING 迁移成 QUEUED，让**数据库**成为「谁有权发这条消息」
        的裁决者：并发 reconciler 只有一个能迁移成功，也只有一个投递；已被取消 /
        已处理的 PENDING 谓词不成立，绝不会发出消息。

        投递失败后的最终收敛：此时 run 停在 QUEUED 且 ``next_retry_at IS NULL``，
        由 :meth:`list_recoverable_runs` 的 QUEUED 孤儿分支（同样带宽限期）兜底。
        因此这条路径**总能**收敛到执行或 DLQ，不存在永久搁浅的 PENDING。

        返回迁移后的 run；谓词不成立返回 ``None``（benign no-op）。
        """
        now = now or _utcnow()
        if not self.repo.promote_pending_to_queued(run_id, now=now):
            return None
        return self.repo.get(run_id)

    def list_stale_running_runs(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """列出 lease 已过期、owner 已失效的 RUNNING run（含 attempt 预算）。

        这些 run 不会再被任何其它机制捞起：broker 已 ACK，``list_recoverable_runs``
        也不看 RUNNING。没有本方法，它们会永久停在 RUNNING。
        """
        return self.repo.list_stale_running_runs(limit=limit, now=now or _utcnow())

    def dead_letter_stale_running(
        self, run_id: str, *, error_code: str, error_message: str, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """把「lease 仍过期 **且** 预算已耗尽」的 RUNNING run 原子推入 DEAD_LETTER。

        与 :meth:`mark_dead_letter` 的区别：这里**不**做 ``worker_id`` 匹配
        （owner 本来就已失效，没有 owner 可匹配），但**要求 lease 仍然过期**
        —— 一个刚刚续上 lease 的健康 run 不会被 reconciler 误杀；**同时要求预算
        已经耗尽**，否则应当走接管而不是落 DLQ。

        三条谓词（``status=RUNNING`` / ``lease_expires_at <= now`` /
        ``attempt >= max_attempts``）与 DLQ 行写入都在**同一条 UPDATE + 同一
        事务**内完成，因此不存在「状态已终态但 DLQ 行缺失」的不可恢复窗口。

        返回 ``None`` 表示谓词不成立（run 已被接走 / 已续租 / 已终态 / 预算未
        耗尽），调用方应把它当作 benign no-op，而不是错误。
        """
        now = now or _utcnow()
        self.require_run(run_id)  # 不存在则抛 RunNotFound；存在与否由下面的谓词决定
        return self.repo.dead_letter_stale_running(
            run_id,
            now=now,
            error_code=error_code,
            error_message=error_message[:2000],
            error_type="transient",
        )

    def get_dead_letter(self, run_id: str) -> dict[str, Any] | None:
        return self.repo.get_dead_letter(run_id)

    def list_dead_letters(self, limit: int = 100) -> list[dict[str, Any]]:
        """管理员观测：DEAD_LETTER 记录（脱敏由序列化层负责）。"""
        return self.repo.list_dead_letters(limit=limit)

    # ---- 状态迁移 ----

    def _transition(
        self,
        run_id: str,
        target: RunStatus,
        *,
        from_statuses: set[RunStatus] | frozenset[RunStatus],
        **fields: Any,
    ) -> dict[str, Any]:
        updated = self.repo.transition(
            run_id, from_statuses=from_statuses, to_status=target, **fields
        )
        if updated is None:
            current = self.repo.get(run_id)
            if current is None:
                raise RunNotFound(run_id)
            raise InvalidRunTransition(run_id, current["status"], target.value)
        return updated

    def _transition_owned(
        self,
        run_id: str,
        target: RunStatus,
        *,
        from_statuses: set[RunStatus] | frozenset[RunStatus],
        expected_worker_id: str,
        require_live_lease: bool = True,
        **fields: Any,
    ) -> dict[str, Any]:
        """worker-owned 提交：status + owner (+ 存活 lease) 同一条 UPDATE 判定。

        写不进去时区分三种原因：run 不存在 / 状态机不接受 / ownership 丢失。
        第三种抛 :class:`RunOwnershipLost` 而不是 ``InvalidRunTransition``，
        因为它不是业务失败。
        """
        updated = self.repo.transition_owned(
            run_id,
            from_statuses=from_statuses,
            to_status=target,
            expected_worker_id=expected_worker_id,
            require_live_lease=require_live_lease,
            **fields,
        )
        if updated is not None:
            return updated
        current = self.repo.get(run_id)
        if current is None:
            raise RunNotFound(run_id)
        if current["status"] not in {s.value for s in from_statuses}:
            raise InvalidRunTransition(run_id, current["status"], target.value)
        raise RunOwnershipLost(
            run_id,
            expected_worker_id=expected_worker_id,
            current_status=current["status"],
            current_worker_id=current.get("worker_id"),
        )

    def mark_running(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
        task_id: str | None = None,
        lease_seconds: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """QUEUED/RETRYING/RUNNING -> RUNNING（递增 attempt + 写 worker lease）。

        返回 None 表示「不应执行」。三条**互斥**的收敛契约，全部由数据库层的
        条件 UPDATE 判定，不在本方法里先 SELECT 再判断：

        - **RUNNING + 有效 lease**（重复投递）：不改状态，返回 None。
          判定在 ``repository.takeover_running`` 的 lease 谓词里。
        - **RUNNING + 过期 lease + 预算耗尽**：原子收敛 DEAD_LETTER。
          判定在 ``repository.dead_letter_stale_running`` 的
          ``lease_expires_at <= now AND attempt >= max_attempts`` 里。
        - **RUNNING + 过期 lease + 预算未耗尽**：原子接管（attempt+1）。
        - **已终态**（收敛晚于本次投递）：返回 None（benign no-op），不抛异常。

        QUEUED/RETRYING 领取走 ``repository.claim_executable``：状态 + 预算 +
        ``attempt + 1`` 同一条 UPDATE，落败者**不消耗预算**。预算耗尽由
        ``repository.dead_letter_exhausted_executable`` 原子收敛。

        为什么不能把预算判定放在本方法里
        --------------------------------
        ``attempt == max_attempts`` 是**最后一次正常执行**的常态。若在这里先判预算、
        耗尽就无条件 RUNNING -> DEAD_LETTER，则任何一条重复投递（broker 重投、
        reconciler 补投）都会把**持有有效 lease、正在跑图**的 run 打成 DEAD_LETTER：
        已被证实的缺陷。lease 失效与预算耗尽必须都是数据库层谓词，且二者**与**
        状态谓词同在一条 UPDATE 内判定。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        now = now or _utcnow()

        if cur == RunStatus.RUNNING:
            if worker_id is None or lease_seconds is None:
                return run
            # 1) 原子接管：lease 失效谓词 + 预算谓词同在一条 UPDATE 内
            taken = self.repo.takeover_running(
                run_id,
                worker_id=worker_id,
                task_id=task_id,
                lease_seconds=lease_seconds,
                now=now,
            )
            if taken is not None:
                return taken
            # 2) 未取得所有权。只有「lease 仍然过期 **且** 预算已耗尽」才允许收敛
            #    到 DLQ；持有有效 lease 的执行在这里被完整放过（benign no-op）。
            self._dead_letter_budget_exhausted(
                run_id,
                from_statuses={RunStatus.RUNNING},
                source="RUNNING 接管",
                require_expired_lease=True,
                now=now,
            )
            # 3) 别人持有有效 lease / 并发竞争输了 / 已终态 -> benign no-op
            return None

        if cur in TERMINAL_STATUSES:
            # 重复投递**晚于**收敛：run 已经是 SUCCEEDED / FAILED / DEAD_LETTER /
            # CANCELLED。正确答案是「本次投递不执行」，而不是抛
            # ``InvalidRunTransition`` —— 终态是合法且预期的收敛结果，把它报成
            # 「非法状态迁移」既不准确，也会打断任何不恰好捕获该异常的调用方。
            # 并发下这是真实可达的：两个重复投递里，第一个把预算耗尽的 run 收敛到
            # DEAD_LETTER，第二个随后才读到终态。
            logger.info("run=%s 已处于终态 %s，重复投递不执行", run_id, cur.value)
            return None

        ensure_transition(cur, RunStatus.RUNNING, run_id)
        claimed = self.repo.claim_executable(
            run_id,
            from_statuses={cur},
            worker_id=worker_id,
            task_id=task_id,
            lease_seconds=lease_seconds,
            set_started_at=run.get("started_at") is None,
            now=now,
        )
        if claimed is not None:
            return claimed
        # 领取落败：区分「预算耗尽（可原子收敛）」与「并发竞争输了（不改状态）」
        if self._dead_letter_budget_exhausted(
            run_id,
            from_statuses={cur},
            source=f"{cur.value} 领取",
            require_expired_lease=False,
            now=now,
        ):
            return None
        current = self.repo.get(run_id)
        if current is None:
            raise RunNotFound(run_id)
        if current["status"] == RunStatus.RUNNING.value:
            # 并发重复投递：竞争者刚把 QUEUED/RETRYING 领取成 RUNNING。这与
            # 「RUNNING 分支里他人持有有效 lease」是同一种良性结果——本次投递不
            # 执行，也不该被报成状态机错误（``RUNNING -> RUNNING`` 根本不是非法
            # 迁移，是这里的比较基于过期快照）。
            return None
        raise InvalidRunTransition(run_id, current["status"], RunStatus.RUNNING.value)

    def _dead_letter_budget_exhausted(
        self,
        run_id: str,
        *,
        from_statuses: set[RunStatus] | frozenset[RunStatus],
        source: str,
        require_expired_lease: bool,
        now: datetime,
    ) -> bool:
        """attempt 预算耗尽时原子收敛到 DEAD_LETTER；谓词不成立返回 False。

        判定完全在数据库层：

        - ``require_expired_lease=True``（RUNNING 来源）：额外要求
          ``lease_expires_at <= now``，因此**正在正常执行的 run 不会被这条路径
          改写**——没有所有权失效证据就不动终态。
        - ``require_expired_lease=False``（QUEUED/RETRYING 来源）：这两类状态没有
          持有中的 lease，谓词为 ``status IN (...) AND attempt >= max_attempts``。

        必须**终态**收敛而不是「拒绝执行就完事」：一旦 reconciler 被调度，
        一个停在 QUEUED/RETRYING 且预算已耗尽的 run 会被 ``list_recoverable_runs``
        （它不比较 ``max_attempts``）反复重新投递，变成无界重投循环。落 DLQ 后它
        不再出现在扫描结果里，循环自然终止，同时留下 ``agent_dead_letters`` 记录
        供人工 ``scripts/replay_dead_run.py`` 重放。

        方向说明：这是 fail-closed——宁可把 run 交给人，也不让它在预算外被执行；
        但只在**已证明 owner 失效**之后才 fail-closed，不误伤活着的执行。
        """
        # 这两个值**只用于构造错误信息**；判定权在数据库的谓词里，所以读到的快照
        # 过期也不会影响收敛决策。
        snapshot = self.require_run(run_id)
        attempt = int(snapshot["attempt"])
        max_attempts = int(snapshot["max_attempts"])
        error_code = "max_attempts_exhausted_at_claim"
        error_message = f"{source}时 attempt 预算已耗尽（{attempt}/{max_attempts}），拒绝再次执行"
        if require_expired_lease:
            updated = self.repo.dead_letter_stale_running(
                run_id,
                now=now,
                error_code=error_code,
                error_message=error_message,
            )
        else:
            updated = self.repo.dead_letter_exhausted_executable(
                run_id,
                from_statuses=from_statuses,
                now=now,
                error_code=error_code,
                error_message=error_message,
            )
        # 只有谓词真的成立才告警：``attempt`` / ``max_attempts`` 只是用于**构造
        # 错误信息**的读值，判定权在数据库。在问数据库之前就打「预算已耗尽」的
        # warning，会让每一次重复投递都产生一条假告警（重复投递在 at-least-once
        # 下是常态），从而淹没真正的预算耗尽信号。
        if updated is not None:
            logger.warning(
                "attempt 预算已耗尽，落 DLQ 拒绝执行: run_id=%s attempt=%d/%d source=%s",
                run_id,
                attempt,
                max_attempts,
                source,
            )
        return updated is not None

    def owns_execution(self, run_id: str, *, worker_id: str) -> bool:
        """当前执行者是否仍持有执行权（**只读**，不续租、不改任何状态）。

        工具在执行副作用前用它复核 ownership。谓词与状态提交同源
        （``repository.owns_execution`` 与 ``transition_owned`` 一致）。

        绝不能用 ``heartbeat`` 代替：那会顺带把租约续期，语义从「检查」变成
        「续租 + 检查」，调用方就再也无法判断自己是否真的还持有执行权。
        """
        return self.repo.owns_execution(run_id, worker_id=worker_id)

    def heartbeat(self, run_id: str, *, worker_id: str, lease_seconds: float) -> bool:
        """续租（仅当前 owner 且 lease 仍存活时生效）。返回是否成功。

        原子 owner CAS：``id + status=RUNNING + worker_id + lease_expires_at > now``
        在同一条 UPDATE 里判定。旧实现是 ``get() -> Python 判 owner -> transition()``，
        那是 TOCTOU：读到自己的 owner 之后、写入之前，别人可能已经接管。

        两个后果都被这条谓词挡住：
          - 已被接管的 stale worker 续租失败，且不会改动新 owner 的任何字段；
          - **已过期但尚未被接管**的 owner 续租同样失败 —— 否则它能靠一次迟到的
            续租给自己续命，lease 就没有严格 expiry 语义了。
        """
        return self.repo.renew_lease_owned(
            run_id,
            expected_worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    def mark_succeeded(
        self,
        run_id: str,
        result: dict[str, Any] | None,
        *,
        expected_worker_id: str | None = None,
    ) -> dict[str, Any]:
        """RUNNING -> SUCCEEDED。

        ``expected_worker_id`` 由 worker 执行路径**必须**传入：只有仍然是当前
        owner 且 lease 存活时才允许提交，失去所有权抛 :class:`RunOwnershipLost`
        且不改动数据库。否则一个已被接管的旧 worker 迟到的成功会覆盖新 owner。

        传 ``None`` 表示「非 worker 提交的外部/管理写入」，保持原有只判 status
        的语义（测试与后台路径使用）。
        """
        if expected_worker_id is None:
            run = self.require_run(run_id)
            ensure_transition(parse_status(run["status"]), RunStatus.SUCCEEDED, run_id)
            return self._transition(
                run_id,
                RunStatus.SUCCEEDED,
                from_statuses={RunStatus.RUNNING},
                result=result or {},
                error_code=None,
                error_message=None,
                error_type=None,
                last_error=None,
                next_retry_at=None,
                lease_expires_at=None,
                finished_at=_utcnow(),
            )
        return self._transition_owned(
            run_id,
            RunStatus.SUCCEEDED,
            from_statuses={RunStatus.RUNNING},
            expected_worker_id=expected_worker_id,
            result=result or {},
            error_code=None,
            error_message=None,
            error_type=None,
            last_error=None,
            next_retry_at=None,
            lease_expires_at=None,
            finished_at=_utcnow(),
        )

    def mark_failed(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        error_type: str = "permanent",
        expected_worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Permanent error -> FAILED（终态，不重试）。

        worker 执行路径必须传 ``expected_worker_id``：stale worker 不能把新
        owner 正在跑的 RUNNING 改成 FAILED。
        """
        if expected_worker_id is None:
            run = self.require_run(run_id)
            ensure_transition(parse_status(run["status"]), RunStatus.FAILED, run_id)
            return self._transition(
                run_id,
                RunStatus.FAILED,
                from_statuses={RunStatus.RUNNING},
                error_code=error_code,
                error_message=error_message[:2000],
                error_type=error_type,
                last_error=error_message[:2000],
                lease_expires_at=None,
                finished_at=_utcnow(),
            )
        return self._transition_owned(
            run_id,
            RunStatus.FAILED,
            from_statuses={RunStatus.RUNNING},
            expected_worker_id=expected_worker_id,
            error_code=error_code,
            error_message=error_message[:2000],
            error_type=error_type,
            last_error=error_message[:2000],
            lease_expires_at=None,
            finished_at=_utcnow(),
        )

    def mark_retrying(
        self,
        run_id: str,
        *,
        delay_seconds: float,
        error_type: str = "transient",
        error_code: str = "",
        error_message: str = "",
        expected_worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Transient error -> RETRYING，写 next_retry_at（退避后重新投递）。

        worker 执行路径必须传 ``expected_worker_id``：否则 stale worker 可以在
        新 owner 执行期间把 RUNNING 改成 RETRYING，并让上层发布一个不该存在的
        重试消息。
        """
        now = _utcnow()
        if expected_worker_id is None:
            run = self.require_run(run_id)
            ensure_transition(parse_status(run["status"]), RunStatus.RETRYING, run_id)
            return self._transition(
                run_id,
                RunStatus.RETRYING,
                from_statuses={RunStatus.RUNNING},
                next_retry_at=now + timedelta(seconds=max(0.0, delay_seconds)),
                error_type=error_type,
                error_code=error_code or None,
                last_error=error_message[:2000] or None,
                lease_expires_at=None,
            )
        return self._transition_owned(
            run_id,
            RunStatus.RETRYING,
            from_statuses={RunStatus.RUNNING},
            expected_worker_id=expected_worker_id,
            next_retry_at=now + timedelta(seconds=max(0.0, delay_seconds)),
            error_type=error_type,
            error_code=error_code or None,
            last_error=error_message[:2000] or None,
            lease_expires_at=None,
        )

    def mark_dead_letter(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        error_type: str = "transient",
        worker_id: str | None = None,
        expected_worker_id: str | None = None,
    ) -> dict[str, Any]:
        """retry 用尽 -> DEAD_LETTER（终态）+ 持久 DLQ 记录。

        按来源区分 ownership（刻意不一刀切）：

        - **RUNNING 来源**：这是 worker 执行路径提交的结果，必须 owner CAS。
          传 ``expected_worker_id`` 生效；stale worker 不能把新 owner 的 run
          推入 DLQ。
        - **RETRYING / QUEUED 来源**：DLQ 重放、reconciler、管理员/API 路径
          （例如 ``POST /api/runs`` 的 dispatch 失败），它们不是 worker ownership
          commit，**不**要求 worker_id 匹配。若一并 owner-gate 会直接打断运维
          闭环。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        owner_gated = cur == RunStatus.RUNNING and expected_worker_id is not None
        if cur in (RunStatus.RUNNING, RunStatus.RETRYING, RunStatus.QUEUED):
            fields = {
                "error_code": error_code,
                "error_message": error_message[:2000],
                "error_type": error_type,
                "last_error": error_message[:2000],
                "lease_expires_at": None,
                "finished_at": _utcnow(),
            }
            if owner_gated:
                updated = self._transition_owned(
                    run_id,
                    RunStatus.DEAD_LETTER,
                    from_statuses={RunStatus.RUNNING},
                    expected_worker_id=expected_worker_id,
                    **fields,
                )
            else:
                updated = self.repo.transition(
                    run_id,
                    from_statuses={cur},
                    to_status=RunStatus.DEAD_LETTER,
                    **fields,
                )
                if updated is None:
                    latest = self.repo.get(run_id)
                    if latest is None:
                        raise RunNotFound(run_id)
                    if latest["status"] == RunStatus.DEAD_LETTER.value:
                        updated = latest
                    else:
                        raise InvalidRunTransition(run_id, latest["status"], "DEAD_LETTER")
        elif cur == RunStatus.DEAD_LETTER:
            updated = run
        else:
            raise InvalidRunTransition(run_id, cur.value, RunStatus.DEAD_LETTER.value)

        self.repo.add_dead_letter(
            run_id=run_id,
            thread_id=run["thread_id"],
            attempt_count=int(run["attempt"]),
            max_attempts=int(run["max_attempts"]),
            error_type=error_type,
            error_code=error_code,
            error_message=error_message,
            worker_id=worker_id or run.get("worker_id"),
        )
        return updated

    def requeue_dead_letter(self, run_id: str, *, reset_attempts: bool = True) -> dict[str, Any]:
        """把 DEAD_LETTER run 重新投递（人工 replay / redrive）。

        刻意**复用原 run_id**，而不是复制出一个新 run：
          - 工具幂等键 ``operation_key = run_id:tool_call_id`` 依赖 run_id 稳定。换
            新 run_id 会让重放绕过 side-effect ledger，把已经成功写入外部系统的
            退款/改单**再执行一次**；这比重放失败严重得多。
          - 规范里的 ``job_id`` 是「一次队列投递」，重放正是新的一次投递，与
            run_id 是两个概念。

        历史保留：``agent_dead_letters`` 行不可变（``add_dead_letter`` 对既有行
        直接返回），因此原始 attempt / error_code / error_type / entered_at 全部留存。
        仅重置可重试字段与 attempt 计数。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur != RunStatus.DEAD_LETTER:
            raise InvalidRunTransition(run_id, cur.value, RunStatus.QUEUED.value)
        fields: dict[str, Any] = {
            "next_retry_at": None,
            "lease_expires_at": None,
            "error_code": None,
            "error_message": None,
            "error_type": None,
            "last_error": None,
            "finished_at": None,
            "worker_id": None,
            "task_id": None,
        }
        if reset_attempts:
            fields["attempt"] = 0
        ensure_declared_transition(RunStatus.DEAD_LETTER, RunStatus.QUEUED, run_id)
        queued = self._transition(
            run_id,
            RunStatus.QUEUED,
            from_statuses={RunStatus.DEAD_LETTER},
            **fields,
        )
        queued_at = queued.get("queued_at")
        if queued_at is not None:
            # 重新入队时间单独刷新，保留首次 queued_at 作为延迟统计起点
            self.repo.touch_queued(run_id, _utcnow())
        return self.repo.get(run_id) or queued

    def defer_run(self, run_id: str, *, delay_seconds: float) -> dict[str, Any] | None:
        """thread 竞争时延迟重调度：保持 QUEUED/RETRYING，仅更新 next_retry_at。

        不消耗 attempt（竞争不是 run 失败）。
        """
        run = self.repo.get(run_id)
        if run is None:
            return None
        cur = parse_status(run["status"])
        if cur not in EXECUTABLE_STATUSES:
            return run
        ensure_declared_transition(cur, cur, run_id)
        now = _utcnow()
        return self._transition(
            run_id,
            cur,
            from_statuses={cur},
            next_retry_at=now + timedelta(seconds=max(0.0, delay_seconds)),
        )

    def mark_queued(self, run_id: str) -> dict[str, Any]:
        """PENDING -> QUEUED（两阶段创建的入队确认）。"""
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur == RunStatus.QUEUED:
            return run
        ensure_transition(cur, RunStatus.QUEUED, run_id)
        return self._transition(
            run_id,
            RunStatus.QUEUED,
            from_statuses={cur},
            queued_at=_utcnow(),
        )

    def mark_waiting_approval(
        self, run_id: str, *, expected_worker_id: str | None = None
    ) -> dict[str, Any]:
        """RUNNING -> WAITING_APPROVAL：高风险副作用已挂起等人工决策。

        幂等：已在 WAITING_APPROVAL 直接返回；在终态或 QUEUED/RETRYING 时不改写
        （抛 ``InvalidRunTransition`` 由调用方收敛）。

        刻意**清空 lease 与 worker_id**：图已挂起、没有 worker 在执行，留着过期
        lease 只会让「谁有权接管」的判断失真；清空后恢复路径可以干净地重新领取。
        也因此 approval 恢复（``mark_resumed_running``）由第一个 worker 通过
        status CAS 领取新 ownership，**不**要求匹配审批前的 worker。

        worker 执行路径必须传 ``expected_worker_id``：失去所有权的旧 worker 不能
        把新 owner 的 run 挂起等审批。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur == RunStatus.WAITING_APPROVAL:
            return run
        ensure_transition(cur, RunStatus.WAITING_APPROVAL, run_id)
        fields: dict[str, Any] = {
            "lease_expires_at": None,
            "next_retry_at": None,
            "worker_id": None,
        }
        if expected_worker_id is None:
            return self._transition(
                run_id,
                RunStatus.WAITING_APPROVAL,
                from_statuses={cur},
                **fields,
            )
        return self._transition_owned(
            run_id,
            RunStatus.WAITING_APPROVAL,
            from_statuses={RunStatus.RUNNING},
            expected_worker_id=expected_worker_id,
            **fields,
        )

    def mark_resumed_running(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
        task_id: str | None = None,
        lease_seconds: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """WAITING_APPROVAL -> RUNNING（人工审批已决策，恢复执行）。**不递增 attempt。**

        为什么必须与 ``mark_running`` 分开
        --------------------------------
        ``mark_running`` 语义是「一次执行尝试」，每次领取都 ``attempt + 1``。
        审批恢复**不是**新的一次失败重试：等人可能耗时数小时，若复用
        ``mark_running``，``AGENT_RUN_MAX_ATTEMPTS``（默认 3）会被「等待审批」
        消耗掉，审批落地前 run 就已 DEAD_LETTER。等待人不是失败。

        并发：条件更新 ``WHERE status='WAITING_APPROVAL'``，多个 worker 同时
        恢复只有一个成功；其余收到 ``InvalidRunTransition`` 并读到 RUNNING。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur == RunStatus.RUNNING:
            return run
        ensure_transition(cur, RunStatus.RUNNING, run_id)
        now = now or _utcnow()
        fields: dict[str, Any] = {
            "worker_id": worker_id,
            "task_id": task_id,
            # attempt 原样保留：审批等待不消耗重试预算
        }
        if lease_seconds is not None:
            fields["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
            fields["heartbeat_at"] = now
        return self._transition(
            run_id, RunStatus.RUNNING, from_statuses={RunStatus.WAITING_APPROVAL}, **fields
        )

    def resume_after_approval(self, run_id: str) -> dict[str, Any]:
        """等待审批的 run 是否已可恢复（供审批 API 判断能否投递）。

        返回该 run 的最新记录；**不改状态**。恢复由 worker 领取时走
        ``mark_resumed_running`` 完成（``WAITING_APPROVAL -> RUNNING``）。

        这里刻意**没有** ``WAITING_APPROVAL -> QUEUED`` 这条边：审批 API 若把 run
        改成 QUEUED，worker 领取就会走 ``mark_running``，而它每次领取都
        ``attempt + 1``；``AGENT_RUN_MAX_ATTEMPTS`` 默认 3，于是「等人审批」会
        被当成三次失败重试，审批还没落地 run 就 DEAD_LETTER。等待人不是失败。
        审批 API 只负责 dispatch，状态由 worker 侧原子迁移。
        """
        return self.require_run(run_id)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """取消尚未完成的 run -> CANCELLED（终态）。

        语义边界（必须诚实说明）：
          - 已进入终态（SUCCEEDED/FAILED/DEAD_LETTER/CANCELLED）-> 不改写，返回现状；
          - QUEUED/RETRYING/PENDING -> 立即置 CANCELLED：worker 领取时会因状态不是
            EXECUTABLE_STATUSES 而跳过，等价于「投递后被丢弃」，不会执行图；
          - RUNNING -> 置 CANCELLED 属于**协作式取消**：只改状态，不中断已在运行的
          asyncio task。worker 的 ``mark_succeeded`` 走
          ``from_statuses={RUNNING}`` 条件更新，因此不会覆盖 CANCELLED；调用方
          必须等待 ``finished_at``/轮询确认最终态，不能假设已中断执行。

        并发：取消用**单条** ``UPDATE ... WHERE status NOT IN (终态)`` 判定。
        先读状态再按该状态做 CAS 会与 promote/领取竞争出假失败（读到 PENDING、
        写入时已变 QUEUED）；取消对每个非终态都合法，所以谓词直接写成「不是终态」，
        不需要预先知道当前状态。
        """
        run = self.require_run(run_id)
        cur = parse_status(run["status"])
        if cur in TERMINAL_STATUSES:
            return run
        if not self.repo.cancel_if_not_terminal(run_id, now=_utcnow()):
            # 谓词不成立：run 在读与写之间进了终态（正常收敛），返回现状。
            latest = self.repo.get(run_id)
            if latest is None:
                raise RunNotFound(run_id)
            if latest["status"] in {s.value for s in TERMINAL_STATUSES}:
                return latest
            raise InvalidRunTransition(run_id, latest["status"], RunStatus.CANCELLED.value)
        return self.repo.get(run_id) or run


_default_service: RunService | None = None


def get_run_service() -> RunService:
    """进程内默认服务（路由使用；测试可注入独立 service）。"""
    global _default_service
    if _default_service is None:
        _default_service = RunService()
    return _default_service


def reset_run_service_for_tests() -> None:
    global _default_service
    _default_service = None
