#!/usr/bin/env python3
"""重新投递卡住的 AgentRun（RETRYING/QUEUED 且退避时间已过）。

主恢复路径是 broker redelivery：``runtime/executor.py`` 在延迟重试**投递失败**时让
异常逃逸，Celery ``acks_late`` 不 ACK，消息会被重新投递。本脚本是**兜底**：处理
消息在 at-least-once 之外彻底消失的情形（broker 重启丢队列、消息被清理、inline 模式
进程内任务丢失），避免 run 永远停在 RETRYING。

用法::

    python scripts/reconcile_stuck_runs.py --dry-run
    python scripts/reconcile_stuck_runs.py --apply --limit 200

建议以 cron / k8s CronJob 低频运行（例如每分钟一次）。

退出码：0 成功；1 执行失败。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main_async(args: argparse.Namespace) -> int:
    from runtime.retry import reconcile_stuck_runs
    from runtime.run_service import RunService

    service = RunService()
    if args.dry_run:
        ids = service.list_recoverable_runs(limit=args.limit)
        print(f"stuck runs (dry-run, 未投递): {len(ids)}")
        for rid in ids[:50]:
            print(f"  - {rid}")
        if len(ids) > 50:
            print(f"  ... 共 {len(ids)} 条")
        return 0

    dispatched = await reconcile_stuck_runs(service, limit=args.limit)
    print(f"re-dispatched {len(dispatched)} stuck run(s)")
    for rid in dispatched[:50]:
        print(f"  - {rid}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="重新投递卡住的 AgentRun")
    parser.add_argument("--apply", action="store_true", help="真正投递（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="只列出（默认行为）")
    parser.add_argument("--limit", type=int, default=100, help="单次最多处理多少条")
    args = parser.parse_args()
    if not args.apply:
        args.dry_run = True
    try:
        return asyncio.run(main_async(args))
    except Exception as e:
        print(f"reconcile 失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
