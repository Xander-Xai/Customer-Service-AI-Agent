#!/usr/bin/env python3
"""回收孤儿 LangGraph checkpoint thread（会话已不存在但图状态仍在）。

背景见 ``core/checkpoint_retention.py``：用户可见的删除已经同步清理 checkpoint，
idle-TTL / 数量淘汰则由会话层独立完成，因此需要一个可运维的孤儿回收命令。

用法::

    # 只看，不删（默认）
    python scripts/purge_orphan_checkpoints.py --dry-run

    # 真正删除（需显式 --apply）
    python scripts/purge_orphan_checkpoints.py --apply --min-age 86400

环境：
    CHECKPOINT_DATABASE_URL / LANGGRAPH_CHECKPOINT_DATABASE_URL / DATABASE_URL
    CHECKPOINT_RETENTION_MIN_AGE_SECONDS（默认 86400）

退出码：0 成功（含 dry-run）；1 执行失败。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _resolve_db_url() -> str:
    for key in (
        "CHECKPOINT_DATABASE_URL",
        "LANGGRAPH_CHECKPOINT_DATABASE_URL",
        "DATABASE_URL",
    ):
        value = os.getenv(key, "").strip()
        if value.startswith(("postgresql://", "postgres://")):
            return value
    raise SystemExit(
        "需要 PostgreSQL URL：请设置 CHECKPOINT_DATABASE_URL / "
        "LANGGRAPH_CHECKPOINT_DATABASE_URL / DATABASE_URL"
    )


def _load_live_session_ids() -> set[str]:
    """当前仍存在的会话 ID（= thread_id）。

    只读 Redis（若配置了 SESSION_STORAGE_BACKEND=redis 且可用）并回落到内存会话
    管理器；两者都拿不到时抛错——宁可让人显式处理，也不要凭猜测删除历史。
    """
    from core.config import DEV_MODE, REDIS_URL

    ids: set[str] = set()
    if REDIS_URL:
        try:
            import redis

            client = redis.Redis.from_url(
                REDIS_URL, decode_responses=True, socket_timeout=3
            )
            try:
                for key in client.scan_iter(match="session:*", count=500):
                    if key.startswith("session:"):
                        ids.add(key.split(":", 1)[1])
            finally:
                client.close()
            if ids:
                return ids
        except Exception:
            pass
    if DEV_MODE:
        try:
            from core.session.session_manager import get_session_manager

            mgr = get_session_manager()
            ids.update(getattr(mgr, "sessions", {}).keys())
        except Exception:
            pass
    if not ids:
        raise SystemExit(
            "无法枚举现存会话（Redis 与内存会话都不可读）。"
            "为避免误删历史，命令已中止；请修复 SESSION_STORAGE/Redis 后重试，"
            "或显式指定 --assume-all-orphans 之外的处理方式。"
        )
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description="回收孤儿 LangGraph checkpoint thread")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="只看，不删（默认行为）")
    parser.add_argument(
        "--min-age",
        type=float,
        default=float(os.getenv("CHECKPOINT_RETENTION_MIN_AGE_SECONDS", "86400")),
        help="孤儿最小保留年龄（秒），默认 86400",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    from core.checkpoint_retention import (
        find_orphans,
        load_checkpoint_threads,
        render_report,
    )

    url = _resolve_db_url()

    async def run() -> int:
        import psycopg

        from core.checkpointer import build_postgres_checkpointer, close_checkpoint_runtime

        known = _load_live_session_ids()

        with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
            threads = load_checkpoint_threads(conn)

        plan = find_orphans(known, threads, min_age_seconds=args.min_age)

        if args.apply:
            rt = await build_postgres_checkpointer(url, min_size=1, max_size=2)
            saver = rt.checkpointer
            try:
                for candidate in plan.candidates:
                    try:
                        await saver.adelete_thread(
                            {"configurable": {"thread_id": candidate.thread_id}}
                        )
                        plan.deleted.append(candidate.thread_id)
                    except Exception as e:
                        plan.failed[candidate.thread_id] = type(e).__name__
            finally:
                await close_checkpoint_runtime(rt)

        print(render_report(plan, as_json=args.json))
        if args.apply:
            print(f"done: deleted={len(plan.deleted)} failed={len(plan.failed)}")
        return 1 if plan.failed else 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
