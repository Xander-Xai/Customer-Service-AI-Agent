"""Durable logical-operation identity resolution across partial node resume.

Why this module exists
----------------------
A node may execute several **parameter-identical** side-effect writes that belong
to independent business intents (two legitimate 100-yuan refunds of the same
order). ``agents.base_agent`` used to derive each write's logical identity from
an **in-node occurrence counter** (0, 1, ...). That counter is a local variable:
when a worker crashes mid-loop and the whole LangGraph node is replayed, it
restarts at 0. If the replay proposes only the *second* write, it is renumbered
to occurrence 0 and collides with the first (already-SUCCEEDED) key -- the ledger
returns the first write's stored result and the second legitimate write is
**silently lost**.

The occurrence counter is not recoverable from the LLM output alone, so identity
must be anchored to something durable. This resolver:

1. snapshots, per ``(scope, tool, content)``, the ledger rows that already exist
   **at the start of this attempt** (prior attempts' proposals);
2. resolves each call of this attempt against them, in order:

   * a prior **unexecuted** occurrence (PENDING/FAILED, e.g. an interrupted
     proposal) -> reuse it (execute the interrupted operation);
   * else an unassigned prior **SUCCEEDED** occurrence -> reuse it (true replay,
     dedupe);
   * else allocate a **new** occurrence beyond every prior one;

3. durably **reserves** every newly-allocated occurrence *before* the caller
   executes any of them, so a crash later in the round leaves the whole
   proposal set recorded (see ``SideEffectStore.reserve_operation``).

Because the proposal snapshot is taken once per attempt and only *prior* rows are
eligible for reuse, a fresh attempt that proposes several identical writes still
executes all of them (``next_new`` keeps growing), while a replay of a completed
attempt dedupes them all.

This is best-effort identity, not magic: a content-identical proposal that has no
prior trace at all is indistinguishable from a replay, and is treated as one for
the same reason the ledger exists -- at-least-once delivery must not re-execute a
committed write. The caller can always disambiguate with an explicit, durable
``logical_operation_id`` (HITL approvals, external business keys).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.logger import get_logger

logger = get_logger("runtime.operation_identity")

#: 每个 ``(scope, tool, content)`` 最多解析的 occurrence 数。超过即判为异常历史
#: （身份无法确认），fail-closed 而不是静默生成新 occurrence 继续执行。
MAX_CONTENT_OCCURRENCES = 64

STATUS_SUCCEEDED = "SUCCEEDED"


class OperationIdentityUnavailable(RuntimeError):
    """无法为一次写操作确定稳定身份：必须 fail-closed，不得裸执行。"""


@dataclass
class _ContentState:
    """某个 ``(tool, content)`` 在**本次尝试开始时**的持久化身份快照。"""

    #: 存在但未成功（PENDING / FAILED）的 occurrence，升序；优先恢复它们。
    unexecuted: list[int] = field(default_factory=list)
    #: 已成功、尚未被本次尝试消费的 occurrence，升序；用于真重放去重。
    executed: list[int] = field(default_factory=list)
    #: 下一个全新 occurrence（在所有已存在 occurrence 之上）。
    next_new: int = 0


class ContentOccurrenceResolver:
    """把一次节点尝试里的写操作解析到持久化的内容寻址 occurrence。"""

    def __init__(
        self,
        *,
        run_id: str,
        node_id: str,
        agent: str,
        store: Any,
        thread_id: str | None = None,
        max_occurrences: int = MAX_CONTENT_OCCURRENCES,
    ) -> None:
        self._run_id = run_id
        self._node_id = node_id
        self._agent = agent
        self._store = store
        self._thread_id = thread_id
        self._max = int(max_occurrences)
        self._states: dict[tuple[str, str], _ContentState] = {}

    def _candidate_keys(self, tool_name: str, arguments: dict[str, Any]) -> list[str]:
        from runtime.side_effects import build_replay_stable_operation_key

        return [
            build_replay_stable_operation_key(
                run_id=self._run_id,
                node_id=self._node_id,
                agent=self._agent,
                tool_name=tool_name,
                arguments=arguments,
                ordinal=occ,
            )
            for occ in range(self._max)
        ]

    def _load(self, tool_name: str, arguments: dict[str, Any]) -> _ContentState:
        keys = self._candidate_keys(tool_name, arguments)
        statuses = self._store.get_statuses(tool_name, keys)
        state = _ContentState()
        max_occ = -1
        for occ, key in enumerate(keys):
            status = statuses.get(key)
            if status is None:
                continue
            max_occ = max(max_occ, occ)
            if status == STATUS_SUCCEEDED:
                state.executed.append(occ)
            else:
                state.unexecuted.append(occ)
        state.unexecuted.sort()
        state.executed.sort()
        state.next_new = max_occ + 1
        return state

    def assign(self, proposals: list[tuple[str, dict[str, Any]]]) -> list[int]:
        """为一批（按执行顺序排列的）写操作提案分配 occurrence 并预占新身份。

        必须在**执行任何写之前**调用，且传入本轮全部写操作提案——这样一次尝试的
        提案集合会在崩溃前完整落账（reservation），恢复时才能区分「真重放」与
        「被中断的第二次合法操作」。
        """
        from runtime.side_effects import request_fingerprint

        # In mixed history a shortened same-content replay is fundamentally
        # ambiguous: A1 succeeded, A2 was reserved, and a lone A could mean
        # either "replay A1" or "execute A2". Prioritizing PENDING would
        # accidentally execute A2 in the former case. Refuse without a
        # durable logical_operation_id instead of guessing business intent.
        from collections import Counter

        proposal_counts = Counter(
            (tool_name, request_fingerprint(arguments))
            for tool_name, arguments in proposals
        )
        for content_key, count in proposal_counts.items():
            tool_name, _ = content_key
            state = self._states.get(content_key)
            if state is None:
                sample_args = next(
                    args
                    for name, args in proposals
                    if (name, request_fingerprint(args)) == content_key
                )
                state = self._load(tool_name, sample_args)
                self._states[content_key] = state
            if state.executed and state.unexecuted:
                known_count = len(state.executed) + len(state.unexecuted)
                if count < known_count:
                    raise OperationIdentityUnavailable(
                        f"tool {tool_name}: ambiguous partial replay of "
                        f"{count} identical proposals against {known_count} "
                        "prior occurrences; require a durable logical_operation_id"
                    )

        assignments: list[int] = []
        to_reserve: list[tuple[str, str, str]] = []  # (tool_name, key, fingerprint)
        for tool_name, arguments in proposals:
            content_key = (tool_name, request_fingerprint(arguments))
            state = self._states.get(content_key)
            if state is None:
                state = self._load(tool_name, arguments)
                self._states[content_key] = state

            # Preserve the original persisted occurrence order whenever
            # a full set of previously proposed writes is replayed. Status
            # is not an identity and must not reorder those slots.
            if state.executed or state.unexecuted:
                if state.executed and (
                    not state.unexecuted or state.executed[0] < state.unexecuted[0]
                ):
                    assignments.append(state.executed.pop(0))
                else:
                    assignments.append(state.unexecuted.pop(0))
                continue

            occ = state.next_new
            if occ >= self._max:
                raise OperationIdentityUnavailable(
                    f"tool {tool_name} 在同一作用域下的同内容 occurrence 数超过上限 "
                    f"{self._max}；无法确认逻辑身份，拒绝生成新的 occurrence 并执行。"
                )
            state.next_new += 1
            key = self._candidate_keys(tool_name, arguments)[occ]
            to_reserve.append((tool_name, key, request_fingerprint(arguments)))
            assignments.append(occ)

        # 先落账、再执行：崩溃发生在执行途中也不会丢失「本尝试提出过哪些写」。
        for tool_name, key, fingerprint in to_reserve:
            self._store.reserve_operation(
                tool_name=tool_name,
                operation_key=key,
                run_id=self._run_id,
                thread_id=self._thread_id,
                fingerprint=fingerprint,
            )
        return assignments


__all__ = [
    "MAX_CONTENT_OCCURRENCES",
    "OperationIdentityUnavailable",
    "ContentOccurrenceResolver",
]
