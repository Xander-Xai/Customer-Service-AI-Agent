"""写操作工具（side effect）幂等 ledger。

at-least-once delivery + application-level idempotency：同一
``(tool_name, operation_key)`` 一旦 SUCCEEDED，重投递/崩溃恢复不得再次执行。
唯一约束在数据库层（``uq_tool_side_effects_operation``），Redis 不参与真相。

operation_key 该怎么取（**当前推荐**）
------------------------------------
**默认用 :func:`build_replay_stable_operation_key`**——它对**节点重放稳定**::

    operation_key = build_replay_stable_operation_key(
        run_id=run_id, node_id=node_id, agent=agent,
        tool_name=tool_name, ordinal=ordinal,
    )

原因：``run_id + ":" + tool_call_id`` 里的 ``tool_call_id`` 由 LLM 生成，
节点在 checkpoint 提交前崩溃并重放时 LLM 被重新调用，可以产生**不同的** id，
于是同一笔逻辑写操作拿到新的 ``operation_key``，唯一约束看不到任何重复，
副作用执行两次（真实复现见
``tests/integration/runtime/test_side_effect_replay_identity_pg.py``）。

``build_tool_idempotency_key``（``run_id + tool_call_id``）现在只用于
**``tool_call_id`` 本身稳定**的场景，主要是 HITL 路径传
``approval:{approval_id}``——审批 id 恒定，键稳定。**保持不变。**
非 HITL 路径不要用它去重跨重放的写操作。

只读工具不经过此路径。

并发边界
--------
``claim()`` / ``renew_claim()`` / ``_commit_terminal_state()`` 三处都把判定与写入
放在**同一条语句**（``SELECT ... FOR UPDATE`` 行锁 或 条件 UPDATE）里，判定结果
不受并发影响；``ToolSideEffect`` 没有 ``version_id_col``，ORM 层的
「改对象 + commit」只会发出按主键的 UPDATE，因此终态提交必须走条件 UPDATE
（见 :meth:`SideEffectStore._commit_terminal_state` 与
``tests/integration/runtime/test_side_effect_commit_fencing_pg.py``）。

边界：本 ledger 只防止「同一 Agent 重复发起同一副作用」。若下游外部系统需要
端到端幂等，必须由下游 API 接受 idempotency key，或配合人工对账。**不构成下游
ERP 的端到端 exactly-once。**
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.logger import get_logger
from db.database import get_db_session
from db.models import ToolSideEffect

logger = get_logger("runtime.side_effects")

STATUS_PENDING = "PENDING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"

CLAIM_EXECUTE = "execute"
CLAIM_SUCCEEDED = "succeeded"
CLAIM_CONFLICT = "conflict"
CLAIM_IN_PROGRESS = "in_progress"

#: ``tool_side_effects.error_type`` 的列宽（``db/models.py`` / alembic 004）。
ERROR_TYPE_MAX_LEN = 16

#: PENDING 记录的认领租约：租约未过期时**不允许**第二个执行者重复触发副作用，
#: 必须抛 transient 让 run 退避重投；租约过期（认领后崩溃）才允许接管重放。
#:
#: 租约**不是**这个值的函数就能保证安全：单次副作用的执行上界由
#: ``TOOL_EXECUTION_TIMEOUT_SECONDS`` 决定，因此本值必须大于它（见
#: ``min_claim_ttl_seconds`` 与 ``core.config.validate_timeout_invariants``）。
#: 执行期间还会由 :class:`_ClaimRenewer` 周期性续租。
DEFAULT_CLAIM_TTL_SECONDS = 60.0

#: claim TTL 的**下界**系数：必须大于单次副作用的执行上界，留出续租周期与抖动余量。
#: 启动期强制（不满足 -> 拒绝启动），运行期也 fail-closed（见
#: ``execute_idempotent_operation``）。
CLAIM_TTL_MIN_MARGIN_SECONDS = 30.0


def min_claim_ttl_seconds(tool_timeout_seconds: float) -> float:
    """受保护副作用所需的最小 claim TTL = 工具执行上界 + 余量。

    显式函数而不是到处写字面量：这条不变量同时被启动校验
    （``core.config.validate_timeout_invariants``，它**反向读取**本模块的常量）与运行期
    fail-closed 使用，必须同源，否则会一边收紧一边放行。

    依赖方向是单向的：本模块持有真相源，config 读取它。写成 config -> runtime 或
    runtime -> config 都会形成真正的 import 环。
    """
    return float(tool_timeout_seconds) + CLAIM_TTL_MIN_MARGIN_SECONDS


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _claim_lease_expired(row: Any, ttl_seconds: float) -> bool:
    """认领租约是否已过期（认领者崩溃后可被接管重放）。

    优先使用显式的 ``claim_expires_at``；老数据没有该列时回退到 ``updated_at + ttl``。
    两者都缺失时视为**未过期**（保守：宁可让调用方退避重试，也不要误判成可接管而
    重复执行副作用）。
    """
    expires_at = getattr(row, "claim_expires_at", None)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return bool(_utcnow() >= expires_at)
    updated_at = getattr(row, "updated_at", None)
    if updated_at is None:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return bool((_utcnow() - updated_at).total_seconds() > max(0.0, ttl_seconds))


def _new_owner() -> str:
    """每次认领的唯一 owner token（host:pid:uuid 前缀便于排障）。"""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def build_tool_idempotency_key(run_id: str, tool_call_id: str) -> str:
    """工具侧幂等键 = run_id + tool_call_id（接口约定）。

    ⚠️ 这个键**只在 ``tool_call_id`` 稳定时**成立，而 LLM 生成的 tool call id 在
    **节点重放**时并不稳定（见 :func:`build_replay_stable_operation_key`）。

    - HITL 路径传 ``approval:{approval_id}``：审批 id 恒定，键稳定。**保持不变。**
    - 非 HITL 路径传 LLM 的 tool call id：重放会变，键随之失效。**不要用于需要
      跨重放去重的写操作。**
    """
    return f"{run_id}:{tool_call_id}"


#: 重放稳定键的版本前缀。键空间与 ``run_id:tool_call_id`` 隔离，因此
#: HITL（``approval:``）与历史键不受影响。
REPLAY_STABLE_KEY_VERSION = "v1"


def build_replay_stable_operation_key(
    *,
    run_id: str,
    node_id: str,
    agent: str,
    tool_name: str,
    ordinal: int,
) -> str:
    """构造**跨节点重放稳定**的逻辑操作身份（不使用 LLM 的 tool_call_id）。

    为什么不能只用 ``tool_call_id``
    -------------------------------
    ``tool_call_id`` 由 LLM 生成。节点在 checkpoint 提交前崩溃并被重放时，LLM 被
    重新调用，可以产生**不同的** id，于是同一笔逻辑写操作拿到新的
    ``operation_key``，``tool_side_effects`` 的唯一约束看不到任何重复，副作用执行
    两次（真实复现见
    ``tests/integration/runtime/test_side_effect_replay_identity_pg.py``）。

    为什么不用 ``tool_name + arguments`` 全局去重
    -------------------------------------------
    那会把两次**合法**的业务操作合并：用户两次各退款 100 元会被当成同一笔，
    第二次被静默吞掉。业务身份必须包含作用域。

    五个组成部分各自排除一类错误合并
    --------------------------------
    - ``run_id``：不同 run = 用户主动再次发起的新操作，必须独立执行；
    - ``node_id``：同一 run 内不同图节点发起的同参数调用是不同操作；
    - ``agent``：同一节点内不同 agent 角色（协作模式下的分工）是不同操作；
    - ``tool_name``：不同工具不是同一操作；
    - ``ordinal``：同一节点内**同一工具**的第 n 次逻辑调用。LLM 可以在一个节点里
      发出多次同类写操作，ordinal 把它们区分开。

    为什么 ordinal 漂移**不是**完整幂等
    ----------------------------------
    ordinal 是「本节点内该工具」的调用序号，不是逻辑操作的身份。重放时 LLM 若
    产生不同的调用序列，可能把同一笔写移到**另一个槽位**：

    - 插入**另一种只读工具**（例如重放时先查一次 ``query_order``）不影响该写工具的
      槽位：计数器按工具名分槽（``agents/base_agent.py``），``query_order`` 有自己
      的计数；
    - 但**同一写工具**的调用序列发生重排（插入/交换另一次同类写）时，同一笔写会换到
      不同 ordinal，于是拿到**不同的 operation_key**。此时 ``request_fingerprint``
      的同键异参检查看不到它（两次落在两个不同的键上），该写可能被**重复执行**。

    因此 ordinal 的不稳定性只保证「同一槽位内的漂移 fail-closed（同键异参抛
    ``PermanentError``）」，**不保证**「不会重复执行」。需要跨任意写序列重放的
    严格去重时，必须由调用方提供稳定的逻辑操作身份（HITL 路径用
    ``approval:{approval_id}`` 即属此类），不得依赖 ordinal。真实复现与残余风险见
    ``docs/limitations.md`` 与 Issue #12。

    不含 arguments：参数属于指纹（``request_fingerprint``）的职责，混进键里会让
    「同键异参」无法被检测。
    """
    scope = "|".join(
        [
            REPLAY_STABLE_KEY_VERSION,
            run_id,
            node_id,
            agent,
            tool_name,
            str(int(ordinal)),
        ]
    )
    # 键需要确定性且适合做索引；显式限定分隔符歧义（node/agent 名里可能含 "|"）
    # 由上游节点名约定保证，digest 让长度不随名称增长。
    return f"{REPLAY_STABLE_KEY_VERSION}:{hashlib.sha256(scope.encode('utf-8')).hexdigest()}"


def request_fingerprint(arguments: dict[str, Any] | None) -> str:
    payload = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_operation_key(tool_name: str, arguments: dict[str, Any] | None) -> str:
    """无显式 operation_key 时的兜底：对 tool_name + 参数取哈希。"""
    return f"{tool_name}:{request_fingerprint(arguments)}"


def _serialize_reference(result: Any) -> Any:
    if result is None or isinstance(result, dict | list | str | int | float | bool):
        return result
    return str(result)[:2000]


def _clamp_error_type(error_type: str) -> str:
    """把 ``error_type`` 裁到列宽，保证 ledger 的失败记录**一定能写进去**。

    ``error_type`` 是诊断信息，不是业务状态。让诊断字符串的宽度反过来决定「失败
    有没有被记下来」是危险的：写入失败会把行留在 ``PENDING`` + 有效租约，之后的
    重投只会拿到 ``CLAIM_IN_PROGRESS``，一次可恢复的失败被烧成 DLQ，而外部写的
    结果永远无记录。完整类名另有归宿——``error_message`` 是 ``Text``，不裁。
    """
    return (error_type or "")[:ERROR_TYPE_MAX_LEN]


@dataclass
class SideEffectClaim:
    state: str  # execute | succeeded | conflict | in_progress
    result: Any = None
    #: 持有 CLAIM_EXECUTE 的认领 token；写结果时必须携带，用于归属校验。
    owner: str | None = None


class SideEffectStore:
    def __init__(self, session_factory: Callable[[], Any] | None = None):
        self._session_factory = session_factory or get_db_session

    def _session(self):
        return self._session_factory()

    def get(self, tool_name: str, operation_key: str) -> dict[str, Any] | None:
        session = self._session()
        try:
            row = session.execute(
                select(ToolSideEffect).where(
                    ToolSideEffect.tool_name == tool_name,
                    ToolSideEffect.operation_key == operation_key,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "tool_name": row.tool_name,
                "operation_key": row.operation_key,
                "status": row.status,
                "request_fingerprint": row.request_fingerprint,
                "result_reference": row.result_reference,
                "run_id": row.run_id,
            }
        finally:
            session.close()

    def claim(
        self,
        *,
        tool_name: str,
        operation_key: str,
        run_id: str,
        thread_id: str | None,
        fingerprint: str,
        claim_ttl_seconds: float = DEFAULT_CLAIM_TTL_SECONDS,
        owner: str | None = None,
    ) -> SideEffectClaim:
        """原子地声明一次副作用执行（**互斥**）。

        并发安全的关键（来自 PR review 的真实缺陷）：
          过去是 "SELECT -> 判断 -> UPDATE" 三步。两个 worker 可以同时读到同一条
          PENDING 行、同时判定可执行、同时拿到 ``CLAIM_EXECUTE``，于是同一笔退款被
          执行两次；唯一约束只防重复行，防不住重复执行。

        现在分三步，每步都是原子的：
          1. ``INSERT ... ON CONFLICT DO NOTHING``：不存在则原子创建并**立刻持有**
             认领；已存在则说明有人在处理；
          2. ``SELECT ... FOR UPDATE``：把竞争者串行化（行锁）；
          3. 持锁重读后判定：SUCCEEDED 同指纹 -> 返回已存结果；SUCCEEDED 异指纹 ->
             冲突；PENDING 且租约未过期 -> ``in_progress``；PENDING 租约已过期 /
             FAILED -> 改写 ``claim_owner`` 为自己并执行。

        ``owner`` 是本次认领的唯一 token（缺省自动生成）。只有持有当前 ``claim_owner``
        的执行者才能写入成功/失败结果，避免两个执行者互相覆盖。

        **第 3 步的写入安全依赖第 2 步的行锁**——接管（改写 ``claim_owner`` / 状态 /
        租约）发生在持有 ``SELECT ... FOR UPDATE`` 的同一个事务里，提交时一并释放。
        这与终态提交不同：终态提交不能靠行锁 + ORM flush（``ToolSideEffect`` 没有
        ``version_id_col``，flush 只按主键），所以那边必须用条件 UPDATE
        （:meth:`_commit_terminal_state`）。**不要**把这里改写成「先普通 SELECT 判断、
        再按主键 UPDATE」——那会把两个执行者同时判为可执行。

        返回：
          - ``execute``      只有**一个**调用者会拿到
          - ``succeeded``    已完成，返回历史结果（不重复执行）
          - ``conflict``     同 key 不同参数
          - ``in_progress``  另一执行者持有未过期认领；**或**当前方言无行级锁、
            无法安全接管（非 PostgreSQL 的过期/FAILED 行一律 fail-closed 到此态）
        """
        owner = owner or _new_owner()
        now = _utcnow()
        session = self._session()
        try:
            # ---- 1) 原子插入（不存在时我们就是第一个持有者）----
            dialect = session.bind.dialect.name if session.bind is not None else ""
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                stmt = (
                    pg_insert(ToolSideEffect)
                    .values(
                        run_id=run_id,
                        thread_id=thread_id,
                        tool_name=tool_name,
                        operation_key=operation_key,
                        request_fingerprint=fingerprint,
                        status=STATUS_PENDING,
                        claim_owner=owner,
                        claim_expires_at=now + timedelta(seconds=claim_ttl_seconds),
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[ToolSideEffect.tool_name, ToolSideEffect.operation_key],
                    )
                    .returning(ToolSideEffect.id)
                )
                inserted_id = session.execute(stmt).scalar_one_or_none()
                if inserted_id is not None:
                    session.commit()
                    return SideEffectClaim(CLAIM_EXECUTE, owner=owner)
            else:
                # 非 PostgreSQL（SQLite/测试）：先查再插，唯一约束兜底
                existing = session.execute(
                    select(ToolSideEffect).where(
                        ToolSideEffect.tool_name == tool_name,
                        ToolSideEffect.operation_key == operation_key,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        ToolSideEffect(
                            run_id=run_id,
                            thread_id=thread_id,
                            tool_name=tool_name,
                            operation_key=operation_key,
                            request_fingerprint=fingerprint,
                            status=STATUS_PENDING,
                            claim_owner=owner,
                            claim_expires_at=now + timedelta(seconds=claim_ttl_seconds),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    try:
                        session.commit()
                        return SideEffectClaim(CLAIM_EXECUTE, owner=owner)
                    except IntegrityError:
                        session.rollback()

            # ---- 2) 持行锁串行化竞争者 ----
            row = session.execute(
                select(ToolSideEffect)
                .where(
                    ToolSideEffect.tool_name == tool_name,
                    ToolSideEffect.operation_key == operation_key,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                # 理论上不可达（唯一约束保证行存在）；保守起见不当成执行权。
                return SideEffectClaim(CLAIM_IN_PROGRESS, owner=None)

            # ---- 3) 持锁判定 ----
            if row.status == STATUS_SUCCEEDED:
                if row.request_fingerprint == fingerprint:
                    return SideEffectClaim(CLAIM_SUCCEEDED, row.result_reference, owner=None)
                return SideEffectClaim(CLAIM_CONFLICT, owner=None)

            lease_expired = _claim_lease_expired(row, claim_ttl_seconds)
            if row.status == STATUS_PENDING and not lease_expired:
                return SideEffectClaim(CLAIM_IN_PROGRESS, owner=None)

            # 接管（租约过期 = 原持有者已死；FAILED = 允许重试）只在**能提供行级
            # 互斥**的方言上安全：PostgreSQL 的 ``SELECT ... FOR UPDATE`` 把竞争者
            # 串行化，接管写入与判定在同一事务里提交。
            #
            # SQLite（及任何忽略 ``FOR UPDATE`` 的方言）**没有**这个保证：两个认领者
            # 可以同时读到同一条过期行、同时判定可接管、同时拿到 ``CLAIM_EXECUTE``，
            # 于是同一笔写执行两次（Issue #13）。此处 fail-closed：拒绝交出执行权，
            # 返回 ``in_progress``（调用方 A/B 会当作可重试的 ``TransientError``，
            # 最终进 DLQ 而**不会**重复执行）。首次插入仍走上面的唯一约束分支，
            # 那条路径的互斥由数据库唯一约束提供，与方言无关。
            #
            # 生产另有启动期硬约束禁止非 PostgreSQL 应用库（见
            # ``core/config.py::validate_required_config``），两层互补。
            if dialect != "postgresql":
                logger.warning(
                    "side-effect claim 拒绝在非 PostgreSQL 方言上接管（dialect=%r "
                    "tool=%s op=%s）：该方言无行级锁，无法排除并发接管重复执行。"
                    "已 fail-closed 返回 in_progress。",
                    dialect or "<unknown>",
                    tool_name,
                    operation_key,
                )
                return SideEffectClaim(CLAIM_IN_PROGRESS, owner=None)

            # 接管（租约过期 = 原持有者已死；FAILED = 允许重试）
            row.run_id = run_id
            row.thread_id = thread_id
            row.status = STATUS_PENDING
            row.request_fingerprint = fingerprint
            row.claim_owner = owner
            row.claim_expires_at = now + timedelta(seconds=claim_ttl_seconds)
            row.updated_at = now
            session.commit()
            return SideEffectClaim(CLAIM_EXECUTE, owner=owner)
        except IntegrityError:
            # 并发插入（唯一约束兜底）：回滚后按"别人已持有"处理，绝不返回 EXECUTE。
            session.rollback()
            return SideEffectClaim(CLAIM_IN_PROGRESS, owner=None)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def renew_claim(
        self,
        tool_name: str,
        operation_key: str,
        *,
        owner: str,
        claim_ttl_seconds: float,
    ) -> bool:
        """原子续租：``owner 匹配 AND status=PENDING AND 租约未过期`` 同一条 UPDATE。

        与 ``AgentRunRepository.renew_lease_owned`` 同一语义：

        - ``owner`` 不匹配 -> 拒绝（已被接管，不能给自己续命）；
        - 租约**已过期** -> 拒绝。严格 expiry 语义：一个迟到的续租不能复活一个
          已经被判定为「持有者已死」的认领，否则另一个执行者已经接管，双方都会执行。

        返回是否真的续上了（``rowcount == 1``）。
        """
        now = _utcnow()
        session = self._session()
        try:
            result = session.execute(
                update(ToolSideEffect)
                .where(
                    ToolSideEffect.tool_name == tool_name,
                    ToolSideEffect.operation_key == operation_key,
                    ToolSideEffect.claim_owner == owner,
                    ToolSideEffect.status == STATUS_PENDING,
                    ToolSideEffect.claim_expires_at.isnot(None),
                    ToolSideEffect.claim_expires_at > now,
                )
                .values(claim_expires_at=now + timedelta(seconds=claim_ttl_seconds), updated_at=now)
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

    def commit_result_or_raise(
        self,
        *,
        tool_name: str,
        operation_key: str,
        result_reference: Any,
        owner: str | None,
        request_fingerprint: str | None = None,
    ) -> None:
        """写成功结果；归属校验失败时抛 :class:`SideEffectOwnershipLost`。

        为什么必须抛
        ------------
        外部调用已经返回，但 ledger 写不进结果（执行权已被夺走）意味着**结果未知**。
        旧实现忽略 ``mark_succeeded`` 的返回值就返回结果，上层于是把一次状态未知的
        写操作当成成功：ledger 停在 PENDING，用户看到「退款成功」，而实际状态取决于
        接管者做了什么。

        抛 transient（而不是返回 ``False`` 后继续）是为了让这条路径必然到达上层：
        重试会经过 ledger（对方已提交就直接复用结果），无法确认则最终落 DLQ 人工核对。
        """
        from .errors import SideEffectOwnershipLost

        if not self.mark_succeeded(
            tool_name,
            operation_key,
            result_reference,
            owner=owner,
            request_fingerprint=request_fingerprint,
        ):
            raise SideEffectOwnershipLost(
                f"tool {tool_name} operation_key {operation_key} 的执行权已丢失，"
                "外部调用结果未知；不会报告成功，需经 ledger 重试或人工核对"
            )

    def _commit_terminal_state(
        self,
        *,
        tool_name: str,
        operation_key: str,
        owner: str | None,
        values: dict[str, Any],
        request_fingerprint: str | None = None,
    ) -> bool:
        """用**单条条件 UPDATE** 原子提交终态，返回是否真的提交成功。

        为什么不能是「SELECT 判断 + 改 ORM 对象 + commit」
        -----------------------------------------------
        ``ToolSideEffect`` 没有 ``version_id_col``，所以那条 commit 真实发出的 SQL 是
        ``UPDATE tool_side_effects SET ... WHERE tool_side_effects.id = :pk``——**只按主键**。
        归属判断发生在**另一条语句**里，两条语句之间是可被并发利用的真实窗口：

            A: SELECT 读到 claim_owner=A（通过） -> 暂停
            B: A 的租约过期，B 原子接管，claim_owner=B（B 的租约是新的）
            A: 恢复，按主键提交 -> 覆盖 B 的 PENDING / SUCCEEDED

        真机复现（真实 PostgreSQL + 并发屏障，不用 sleep 赌时序）见
        ``tests/integration/runtime/test_side_effect_commit_fencing_pg.py``。

        同一条 UPDATE 里带上判定谓词，判定与写入就在**一个事务原子步骤**内，
        与同模块 :meth:`renew_claim` 的形态一致。

        三个谓词各自排除一类错误（缺一不可）
        -----------------------------------
        - ``claim_owner == owner``：旧 owner 不能覆盖新 owner。这是最关键的一条——
          接管者的租约是**未过期**的，「租约未过期」谓词挡不住旧 owner，只有它能。
        - ``status == PENDING``：终态不可被迟到写入改写（已 SUCCEEDED 的操作不会被
          迟到的失败回调改成 FAILED）。
        - ``claim_expires_at > now``：过期但尚未被接管的 owner 不能靠一次迟到提交
          **复活**执行权——否则租约语义形同虚设。

        关于 ``request_fingerprint`` 谓词
        ------------------------------
        可选但由 :func:`execute_idempotent_operation` 始终传入：``claim()`` 在接管时会
        **改写** ``request_fingerprint``，所以它始终代表「当前 owner 认领的参数集」。
        带上它 = 「只在这一行仍描述我认领的那组参数时提交」，是对 ABA / token 碰撞的
        纵深防御，并让「结果 ↔ 参数」在同一行内自洽。

        为什么**不**加 version 列谓词
        -----------------------------
        - 我们要的判定就是「我下面的所有权有没有被换掉」，而 ``claim_owner`` 在每次
          接管时原子变化，它**就是**那个 epoch。version 列只会重复同一真相。
        - ``updated_at`` 不能当 version：``renew_claim`` 会更新它而不换 owner，正常续租
          就会误判冲突。
        - owner + status + lease 三条合起来对「迟到写入」比 owner + version **更强**：
          它们额外挡住过期提交与终态改写，而 version 单独用挡不住这两类。
        - 引入 version 列需要迁移，并制造第二份可能漂移的真相源。
        """
        from .errors import PermanentError

        if not owner:
            # fail-closed：没有 owner token 就等于放弃了生产级写入权限检查。
            # 过去这里是「owner 可选」，None 会退化成无条件写入（按主键）——
            # 等于任何调用方都能改写别人的副作用结果。
            raise PermanentError(
                f"tool {tool_name} operation_key {operation_key} 的结果提交缺少 claim owner；"
                "不接受无归属校验的写入（fail-closed）。请使用 claim() 返回的 owner。"
            )

        now = _utcnow()
        session = self._session()
        try:
            stmt = update(ToolSideEffect).where(
                ToolSideEffect.tool_name == tool_name,
                ToolSideEffect.operation_key == operation_key,
                ToolSideEffect.claim_owner == owner,
                ToolSideEffect.status == STATUS_PENDING,
                ToolSideEffect.claim_expires_at.isnot(None),
                ToolSideEffect.claim_expires_at > now,
            )
            if request_fingerprint is not None:
                stmt = stmt.where(ToolSideEffect.request_fingerprint == request_fingerprint)
            result = session.execute(stmt.values(**values))
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

    def mark_succeeded(
        self,
        tool_name: str,
        operation_key: str,
        result_reference: Any,
        *,
        owner: str | None = None,
        request_fingerprint: str | None = None,
    ) -> bool:
        """标记成功。**只有当前未过期认领者**可以写入；返回是否真的写入。

        判定在 UPDATE 的 WHERE 里（见 :meth:`_commit_terminal_state`），所以
        「读到的 owner」与「写入的行」之间不存在 TOCTOU 窗口：旧 owner 无法覆盖
        新 owner 的 PENDING / SUCCEEDED，迟到的异常无法改写已终态的操作。
        """
        now = _utcnow()
        return self._commit_terminal_state(
            tool_name=tool_name,
            operation_key=operation_key,
            owner=owner,
            request_fingerprint=request_fingerprint,
            values={
                "status": STATUS_SUCCEEDED,
                "result_reference": _serialize_reference(result_reference),
                "error_type": None,
                "error_message": None,
                "finished_at": now,
                "updated_at": now,
                # 终态没有活着的认领：清租约，避免留下可被误读为「仍被持有」的行。
                "claim_expires_at": None,
            },
        )

    def mark_failed(
        self,
        tool_name: str,
        operation_key: str,
        *,
        error_type: str,
        error_message: str,
        owner: str | None = None,
        request_fingerprint: str | None = None,
    ) -> bool:
        """标记失败。**只有当前未过期认领者**可以写入；返回是否真的写入。

        判定同样在 WHERE 里，因此迟到的失败回调不能把已 SUCCEEDED 的操作改写成
        FAILED（那会让一次真实成功的写操作在 ledger 上变成「失败」并被重新发起）。

        ``error_type`` 会被裁到 :data:`ERROR_TYPE_MAX_LEN`。
        为什么裁剪是**正确性**要求而不是美观问题：ledger 的失败记录是「重试 /
        DLQ / 人工对账」的依据，一旦这次写入因为诊断字符串过长而失败，行就会以
        ``PENDING`` + **有效租约** 滞留——没有任何东西能清掉它，重投只会拿到
        ``CLAIM_IN_PROGRESS``，一次可恢复的工具失败最终被烧成 DLQ，且外部写是否
        落地永远没有记录。真实 PostgreSQL 复现（``ToolExecutionTimeout`` 20 字符
        写入 ``VARCHAR(16)`` -> ``DataError`` -> 行滞留 PENDING）见
        ``tests/integration/runtime/test_side_effect_commit_fencing_pg.py``。
        """
        now = _utcnow()
        return self._commit_terminal_state(
            tool_name=tool_name,
            operation_key=operation_key,
            owner=owner,
            request_fingerprint=request_fingerprint,
            values={
                "status": STATUS_FAILED,
                "error_type": _clamp_error_type(error_type),
                "error_message": error_message[:2000],
                "updated_at": now,
                "claim_expires_at": None,
            },
        )


_default_store: SideEffectStore | None = None


def get_side_effect_store() -> SideEffectStore:
    global _default_store
    if _default_store is None:
        _default_store = SideEffectStore()
    return _default_store


def reset_side_effect_store_for_tests() -> None:
    global _default_store
    _default_store = None


class _ClaimRenewer:
    """在副作用**执行期间**周期性延长认领租约。

    为什么必须有它
    --------------
    ``claim()`` 用「租约过期」判定持有者已死，从而允许接管重放。若不续租，一次耗时
    超过 claim TTL 的写操作会在**仍在执行**时被第二个执行者接管，同一笔写操作被发起
    两次——归属校验只能保护 ledger 状态，无法撤销已经发出的外部调用。

    与 run lease 心跳的类比
    ----------------------
    同一个模式：``AgentRunRepository.renew_lease_owned`` + executor 的
    ``_heartbeat_loop`` 保护 run 的 ownership lease；这里保护单次工具调用的 claim。
    两者的判定都是「owner 匹配 AND 租约未过期」，因此迟到的续租不能复活一个已被
    判定失效的租约。

    刻意不做的事
    ------------
    - 不吞异常：续租失败（数据库不可用等）只记录日志，**不**中断正在执行的副作用。
      中断会让「外部已写入、结果未知」变得更难判断；正确处理是在结果提交时由归属
      校验暴露为 ``SideEffectOwnershipLost``。
    - 不假设续租一定成功：因此下界校验（``min_claim_ttl_seconds``）与启动期配置
      校验是必需的，续租只是把窗口推后，不是消除它。
    """

    #: 续租周期 = TTL / 该系数。取 3 意味着租约有 3 次续租机会后才会过期，
    #: 足以容忍两次连续的数据库抖动或调度延迟。
    RENEW_DIVISOR = 3.0
    #: 续租周期下界，避免 TTL 很小时把数据库打成高频写。
    MIN_INTERVAL_SECONDS = 0.05

    def __init__(
        self,
        store: SideEffectStore,
        *,
        tool_name: str,
        operation_key: str,
        owner: str | None,
        claim_ttl_seconds: float,
    ):
        self._store = store
        self._tool_name = tool_name
        self._operation_key = operation_key
        self._owner = owner
        self._ttl = claim_ttl_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.interval = max(self.MIN_INTERVAL_SECONDS, claim_ttl_seconds / self.RENEW_DIVISOR)
        self.renewals = 0
        self.failures = 0

    async def start(self) -> None:
        # 极短 TTL 或测试里没有真实租约需要续（owner 为 None）时不启动循环。
        if self._owner is None or self.interval <= 0:
            return
        self._task = asyncio.create_task(self._loop(), name="side-effect-claim-renewer")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                return  # stop() 已置位
            except (asyncio.TimeoutError, TimeoutError):
                pass
            except asyncio.CancelledError:  # pragma: no cover - 取消路径
                return
            try:
                ok = self._store.renew_claim(
                    self._tool_name,
                    self._operation_key,
                    owner=self._owner or "",
                    claim_ttl_seconds=self._ttl,
                )
                if ok:
                    self.renewals += 1
                else:
                    self.failures += 1
                    logger.warning(
                        "side-effect claim 续租失败（执行权可能已丢失）tool=%s op=%s",
                        self._tool_name,
                        self._operation_key,
                    )
            except Exception as e:  # noqa: BLE001 - 续租不得影响正在执行的副作用
                self.failures += 1
                logger.warning(
                    "side-effect claim 续租异常 tool=%s op=%s: %s",
                    self._tool_name,
                    self._operation_key,
                    type(e).__name__,
                )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None


async def execute_idempotent_operation(
    *,
    tool_name: str,
    operation_key: str,
    run_id: str,
    thread_id: str | None,
    arguments: dict[str, Any] | None,
    operation: Callable[[], Any],
    store: SideEffectStore | None = None,
    claim_ttl_seconds: float = DEFAULT_CLAIM_TTL_SECONDS,
    owner: str | None = None,
    min_claim_ttl_seconds: float | None = None,
) -> Any:
    """通用写操作幂等包裹：同一 ``(tool_name, operation_key)`` 只真正执行一次。

    at-least-once execution + application-level idempotency：Tool 执行成功但
    LangGraph 后续失败、整个 run 重试时，第二次调用直接返回已保存结果，
    **不** 再次触发副作用函数。

    并发安全由三件事共同保证，缺一不可：

    1. **唯一索引 + ``claim`` 的原子语义**（行锁 + 归属校验）；
    2. **执行期间续租**（``_ClaimRenewer``）：认领租约在工具执行期间被周期性延长，
       因此「租约过期 = 持有者已死」这个判定不会在工具**仍在执行**时成立。否则第
       二个执行者会在第一个还没写完时接管并重复发起同一笔写操作
       （真实复现见 ``tests/integration/runtime/test_side_effect_claim_lease_pg.py``）；
    3. **数据库原子的结果提交**（``_commit_terminal_state``）：终态写入是一条带
       「所有权 AND 状态 AND 租约」谓词的单条 UPDATE，判定与写入不存在 TOCTOU
       窗口。归属校验失败时 ``commit_result_or_raise`` 抛
       ``SideEffectOwnershipLost``，把「结果未知」上报，而不是静默返回成功
       （真实复现见 ``tests/integration/runtime/test_side_effect_commit_fencing_pg.py``）。

    ``claim_ttl_seconds`` 的下界（``min_claim_ttl_seconds``）
    ------------------------------------------------------
    续租只是把窗口推后，并不消除它：续租周期、数据库可用性、以及外部调用在
    ``asyncio.wait_for`` 取消后仍在飞行的情况都要求租约有**下界**。低于下界时
    fail-closed（``PermanentError``），不创建 ledger 行、不执行副作用。
    """
    from .errors import PermanentError, TransientError, classify_exception

    if min_claim_ttl_seconds is not None and claim_ttl_seconds < min_claim_ttl_seconds:
        raise PermanentError(
            f"tool {tool_name} 的 side-effect claim TTL（{claim_ttl_seconds}s）低于受保护"
            f"副作用执行上界的下界（{min_claim_ttl_seconds}s）；执行期间租约可能过期，"
            "第二个执行者会重复发起同一笔写操作。已拒绝执行（fail-closed）。"
        )

    store = store or get_side_effect_store()
    fingerprint = request_fingerprint(arguments)
    claim = store.claim(
        tool_name=tool_name,
        operation_key=operation_key,
        run_id=run_id,
        thread_id=thread_id,
        fingerprint=fingerprint,
        claim_ttl_seconds=claim_ttl_seconds,
        owner=owner,
    )
    if claim.state == CLAIM_SUCCEEDED:
        from . import metrics

        metrics.record_tool_idempotency_hit()
        return claim.result
    if claim.state == CLAIM_CONFLICT:
        raise PermanentError(f"tool {tool_name} operation_key {operation_key} 已以不同参数执行过")
    if claim.state == CLAIM_IN_PROGRESS:
        raise TransientError(
            f"tool {tool_name} operation_key {operation_key} 正在被另一个执行者处理"
        )

    renewer = _ClaimRenewer(
        store,
        tool_name=tool_name,
        operation_key=operation_key,
        owner=claim.owner,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    await renewer.start()
    try:
        result = operation()
        if hasattr(result, "__await__"):
            result = await result
    except BaseException as e:
        await renewer.stop()
        # error_type 用 runtime 的**规范分类**（transient|permanent|timeout），与
        # agent_runs.error_type 同一套枚举：它有界、可聚合、运维真正查询的字段。
        # 完整类名放进 error_message（Text 列，不裁），信息不丢。
        #
        # ledger 自身的故障（DB 不可用等）不得**替换**工具的真实异常：运维看到的
        # 应该是业务失败原因，而不是一个基础设施错误。这里已经是失败路径，
        # 不存在「上报成功」的风险，因此记录后照常抛出原始异常。
        try:
            store.mark_failed(
                tool_name,
                operation_key,
                error_type=classify_exception(e),
                error_message=f"{type(e).__name__}: {e}",
                owner=claim.owner,
                request_fingerprint=fingerprint,
            )
        except Exception as ledger_error:  # noqa: BLE001 - 不得掩盖原始异常
            logger.error(
                "side-effect 失败结果写入 ledger 失败（原始异常照常上抛）"
                "tool=%s op=%s ledger_error=%s",
                tool_name,
                operation_key,
                type(ledger_error).__name__,
            )
        raise
    await renewer.stop()
    store.commit_result_or_raise(
        tool_name=tool_name,
        operation_key=operation_key,
        result_reference=result,
        owner=claim.owner,
        request_fingerprint=fingerprint,
    )
    return result
