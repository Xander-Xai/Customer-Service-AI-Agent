"""Checkpoint 保留策略（retention）与孤儿线程回收。

问题背景（PR #26 review）：启用 PostgreSQL checkpointer 后，图状态（含用户 query 与
模型回复）会**无限期**留在 ``checkpoints`` 表里。用户可见的
``DELETE /api/sessions/{id}`` 过去只清 SessionManager/Redis，不碰 checkpoint；
idle-TTL 淘汰同理。结果是"删除会话"后对话仍在库里，复用同一 ``session_id``
会把本应删除的历史合并回新请求。

处理方式（显式契约，而不是假装两者已耦合）：

1. **用户可见删除** -> 立即删除对应 checkpoint thread。
   见 ``api/routes/sessions.py::delete_session_checkpoint``。

2. **idle TTL / 数量淘汰** -> 由 ``EnhancedSessionManager`` 独立完成，它不持有
   checkpointer，强行耦合会把 session 层和图运行时绑在一起。因此这里提供
   **孤儿线程回收器**：checkpoint 里存在、但对应会话已不存在的 thread 就是孤儿。

   回收命令::

       python scripts/purge_orphan_checkpoints.py --dry-run
       python scripts/purge_orphan_checkpoints.py --apply

3. **保留窗口**：孤儿不是立刻可删的——刚创建、尚未落 Session 的会话也会短暂表现为
   孤儿（创建与首次 checkpoint 之间存在窗口）。因此只回收
   ``min_age_seconds`` 之前写入的孤儿，默认 24h；``--min-age`` 可调。

本模块只提供**纯逻辑 + 可注入的存取接口**，不做连接管理，便于单测；CLI 负责接线。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class OrphanCandidate:
    thread_id: str
    checkpoint_count: int
    latest_ts: datetime | None
    age_seconds: float | None = None

    @property
    def is_old_enough(self) -> bool:
        return self.age_seconds is not None


@dataclass
class PurgePlan:
    candidates: list[OrphanCandidate] = field(default_factory=list)
    skipped_too_young: list[OrphanCandidate] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def deletable(self) -> list[str]:
        return [c.thread_id for c in self.candidates]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [
                {
                    "thread_id": c.thread_id,
                    "checkpoint_count": c.checkpoint_count,
                    "latest_ts": c.latest_ts.isoformat() if c.latest_ts else None,
                    "age_seconds": c.age_seconds,
                }
                for c in self.candidates
            ],
            "skipped_too_young": [c.thread_id for c in self.skipped_too_young],
            "deleted": self.deleted,
            "failed": self.failed,
        }


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, int | float):
        # LangGraph checkpoint 时间戳是 float epoch（秒）
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def find_orphans(
    known_thread_ids: Iterable[str],
    checkpoint_threads: dict[str, dict[str, Any]],
    *,
    now: datetime | None = None,
    min_age_seconds: float = 86400.0,
) -> PurgePlan:
    """纯逻辑：算出哪些 checkpoint thread 是孤儿、且已超过最小保留窗口。

    ``known_thread_ids``: 当前仍然存在的会话（thread_id == session_id）。
    ``checkpoint_threads``: ``{thread_id: {"count": int, "latest_ts": ...}}``。
    """
    now = now or datetime.now(timezone.utc)
    known = {str(t) for t in known_thread_ids}
    plan = PurgePlan()
    for thread_id, info in checkpoint_threads.items():
        if thread_id in known:
            continue
        latest = _parse_ts(info.get("latest_ts"))
        age = (now - latest).total_seconds() if latest is not None else None
        candidate = OrphanCandidate(
            thread_id=thread_id,
            checkpoint_count=int(info.get("count", 0) or 0),
            latest_ts=latest,
            age_seconds=age,
        )
        if age is None or age < min_age_seconds:
            # 时间未知 -> 保守保留；不足窗口 -> 可能是刚创建的会话
            plan.skipped_too_young.append(candidate)
        else:
            plan.candidates.append(candidate)
    plan.candidates.sort(key=lambda c: c.thread_id)
    return plan


def load_checkpoint_threads(conn: Any) -> dict[str, dict[str, Any]]:
    """从一条 DBAPI 连接统计每个 thread 的 checkpoint 数量与最新时间戳。

    时间戳**不在列上**：``AsyncPostgresSaver.setup()`` 建出的 ``checkpoints`` 表只有
    ``thread_id / checkpoint_ns / checkpoint_id / parent_checkpoint_id / type /
    checkpoint(jsonb) / metadata(jsonb)``，没有 ``checkpoint_ts`` 列。真实 schema 已核对
    （见 tests/integration/runtime/test_checkpoint_setup_and_session_delete.py）。
    checkpoint 的写入时间在 ``checkpoint`` 负载的 ``ts`` 字段（ISO-8601），所以这里用
    ``checkpoint->>'ts'`` 取代不存在的列。
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT thread_id, count(*) AS c, max(checkpoint->>'ts') AS latest "
            "FROM checkpoints GROUP BY thread_id"
        )
        rows = cur.fetchall()
    finally:
        with contextlib.suppress(Exception):
            cur.close()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        # 兼容 tuple / dict row_factory
        if isinstance(row, dict):
            thread_id = row.get("thread_id")
            count = row.get("c")
            latest = row.get("latest")
        else:
            thread_id, count, latest = row[0], row[1], row[2]
        if thread_id is None:
            continue
        out[str(thread_id)] = {"count": int(count or 0), "latest_ts": latest}
    return out


def render_report(plan: PurgePlan, *, as_json: bool) -> str:
    if as_json:
        return json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, default=str)
    lines = [f"孤儿 checkpoint thread（可回收）: {len(plan.candidates)}"]
    for c in plan.candidates:
        lines.append(
            f"  - {c.thread_id}  checkpoints={c.checkpoint_count} " f"age={c.age_seconds:.0f}s"
            if c.age_seconds is not None
            else f"  - {c.thread_id}"
        )
    lines.append(f"因太新/时间未知而保留: {len(plan.skipped_too_young)}")
    for c in plan.skipped_too_young:
        lines.append(f"  - {c.thread_id} age={c.age_seconds}")
    if plan.deleted:
        lines.append(f"已删除: {len(plan.deleted)}")
    if plan.failed:
        lines.append(f"失败: {len(plan.failed)} -> {plan.failed}")
    return "\n".join(lines)


__all__ = [
    "OrphanCandidate",
    "PurgePlan",
    "find_orphans",
    "load_checkpoint_threads",
    "render_report",
]
