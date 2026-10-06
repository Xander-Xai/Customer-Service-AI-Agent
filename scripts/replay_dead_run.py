#!/usr/bin/env python3
"""重放一个 DEAD_LETTER run（人工 redrive 闭环）。

用法::

    python scripts/replay_dead_run.py <run_id> [--yes] [--delay SECONDS]

设计要点（不要改成"新建一个 run"）：

* 重放**复用原 run_id**，只是产生一次新的队列投递（规范里的 ``job_id``）。工具
  幂等键是 ``operation_key = run_id:tool_call_id``；换新 run_id 会绕过
  ``tool_side_effects`` ledger，把已经成功落到外部系统的退款/改单**再执行一次**。
* 原始失败历史**完整保留**：``agent_dead_letters`` 行不可变（``add_dead_letter``
  对已存在的 run 直接返回），attempt / error_code / error_type / entered_at 不会被
  重放改写。重放只重置 run 的可重试字段与 attempt 计数。
* 需要人工确认（``--yes`` 之前会打印将要发生的事），避免误触生产写操作。

退出码：0 = 已重新投递；2 = run 不存在/状态不合法；3 = 入队失败。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _iso(value) -> str | None:
    return value.isoformat() if value else None


async def replay(run_id: str, *, delay: float, assume_yes: bool) -> int:
    from runtime.run_service import RunNotFound, RunService
    from runtime.statuses import InvalidRunTransition

    service = RunService()
    try:
        before = service.require_run(run_id)
    except RunNotFound:
        print(f"[FAIL] run 不存在: {run_id}", file=sys.stderr)
        return 2

    dead = service.get_dead_letter(run_id)
    print("=== 重放前状态 ===")
    print(f"  run_id       : {run_id}")
    print(f"  thread_id    : {before['thread_id']}")
    print(f"  status       : {before['status']}")
    print(f"  attempt      : {before['attempt']}/{before['max_attempts']}")
    print(f"  queued_at    : {_iso(before.get('queued_at'))}")
    print(f"  finished_at  : {_iso(before.get('finished_at'))}")
    if dead:
        print("--- 原始失败历史（agent_dead_letters，重放后仍保留）---")
        print(f"  attempt_count: {dead.get('attempt_count')}")
        print(f"  error_type   : {dead.get('error_type')}")
        print(f"  error_code   : {dead.get('error_code')}")
        print(f"  entered_at   : {_iso(dead.get('entered_at'))}")
        print(f"  worker_id    : {dead.get('worker_id')}")

    if before["status"] != "DEAD_LETTER":
        print(f"[FAIL] 只有 DEAD_LETTER 可重放，当前 {before['status']}", file=sys.stderr)
        return 2

    if not assume_yes:
        print("\n即将把该 run 重新置为 QUEUED 并投递（会真实执行 LangGraph）。")
        answer = input("确认重放? 输入 yes 继续: ").strip()
        if answer.lower() != "yes":
            print("[ABORT] 未确认，未做任何改动", file=sys.stderr)
            return 2

    try:
        updated = service.requeue_dead_letter(run_id)
    except InvalidRunTransition as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2

    print("\n=== 重放后 ===")
    print(f"  status       : {updated['status']}")
    print(f"  attempt      : {updated['attempt']}/{updated['max_attempts']}")
    print(f"  queued_at    : {_iso(updated.get('queued_at'))}")

    from runtime import dispatch

    try:
        await dispatch.dispatch_run(run_id, countdown=delay)
    except TypeError:
        await dispatch.dispatch_run(run_id)
    except Exception as e:
        print(f"[FAIL] 入队失败: {type(e).__name__}", file=sys.stderr)
        return 3

    try:
        from runtime import metrics

        metrics.record_dead_letter_replay()
    except Exception:
        pass

    print(f"[PASS] {run_id} 已重新投递（保留原 run_id，原始 DLQ 历史未改写）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="重放一个 DEAD_LETTER AgentRun（保留原始失败历史）"
    )
    parser.add_argument("run_id", help="要重放的 run_id")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（非交互环境/CI 使用）")
    parser.add_argument("--delay", type=float, default=0.0, help="延迟多少秒后投递（默认立即）")
    args = parser.parse_args()
    return asyncio.run(replay(args.run_id, delay=args.delay, assume_yes=args.yes))


if __name__ == "__main__":
    raise SystemExit(main())
