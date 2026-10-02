"""Gate 1 辅助脚本：真正的**跨进程** checkpoint 读写。

作为独立 Python 进程被 ``test_cross_process_checkpoint.py`` 反复启动，每次都是
全新进程（全新连接池、全新 MemorySaver 都不存在）。绝不在同一进程内建两个
saver 来假装"重启"。

用法::

    python tests/integration/runtime/checkpoint_subprocess.py write <thread_id> <db_url>
    python tests/integration/runtime/checkpoint_subprocess.py read  <thread_id> <db_url>

输出为单行 JSON，便于父进程解析。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, TypedDict

# 作为脚本启动时 sys.path[0] 是本目录，必须显式补上仓库根才能 import core/runtime。
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


class _State(TypedDict, total=False):
    n: int
    trail: str


def _build_graph(checkpointer: Any):
    from langgraph.graph import END, START, StateGraph

    async def first(state: _State) -> dict[str, Any]:
        return {"n": state["n"] + 1, "trail": state.get("trail", "") + "A"}

    async def second(state: _State) -> dict[str, Any]:
        return {"n": state["n"] * 10, "trail": state.get("trail", "") + "B"}

    return (
        StateGraph(_State)
        .add_node("first", first)
        .add_node("second", second)
        .add_edge(START, "first")
        .add_edge("first", "second")
        .add_edge("second", END)
        .compile(checkpointer=checkpointer)
    )


async def write(thread_id: str, db_url: str) -> dict[str, Any]:
    from core.checkpointer import build_postgres_checkpointer

    rt = await build_postgres_checkpointer(db_url, min_size=1, max_size=2)
    if rt.backend != "postgres":
        raise SystemExit(f"FATAL: expected postgres backend, got {rt.backend}")
    graph = _build_graph(rt.checkpointer)
    cfg = {"configurable": {"thread_id": thread_id}}
    out = await graph.ainvoke({"n": 1, "trail": ""}, config=cfg)
    return {
        "op": "write",
        "pid": os.getpid(),
        "thread_id": thread_id,
        "result": out,
        "checkpoint_backend": rt.backend,
    }


async def read(thread_id: str, db_url: str) -> dict[str, Any]:
    from core.checkpointer import build_postgres_checkpointer

    rt = await build_postgres_checkpointer(db_url, min_size=1, max_size=2)
    if rt.backend != "postgres":
        raise SystemExit(f"FATAL: expected postgres backend, got {rt.backend}")
    graph = _build_graph(rt.checkpointer)
    cfg = {"configurable": {"thread_id": thread_id}}
    state = await graph.aget_state(cfg)
    values = dict(state.values) if state is not None else {}

    # 在读回 checkpoint 的同一进程继续执行，证明 lineage 真实可续
    continued: dict[str, Any] = {}
    if values:
        continued = await graph.ainvoke({"n": values["n"], "trail": ""}, config=cfg)

    return {
        "op": "read",
        "pid": os.getpid(),
        "thread_id": thread_id,
        "checkpoint_backend": rt.backend,
        "values": values,
        "continued": continued,
        "fresh_marker": str(uuid.uuid4()),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("write", "read"):
        print("usage: checkpoint_subprocess.py {write|read} <thread_id> <db_url>", file=sys.stderr)
        return 2
    op, thread_id, db_url = argv
    runner = {"write": write, "read": read}[op]
    payload = asyncio.run(runner(thread_id, db_url))
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
