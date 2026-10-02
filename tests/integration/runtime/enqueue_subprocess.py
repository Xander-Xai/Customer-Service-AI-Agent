"""Gate 6 辅助脚本：扮演「API 进程」，只做落库后的入队。

必须是**独立进程**且 ``AGENT_RUN_DISPATCH=celery``：若在测试进程里直接调用
``dispatch_run``，测试进程继承的 ``AGENT_RUN_DISPATCH=inline``（仓库 .env 默认）
会让 run 在「API 侧」被执行，恰好违反 Gate 6 要验证的解耦语义。

用法::

    python tests/integration/runtime/enqueue_subprocess.py <run_id> <queue> <db_url> <redis_url>

输出单行 JSON：``{"run_id":..., "task_id":..., "mode":...}``，或 ``{"error": ...}``。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: enqueue_subprocess.py <run_id> <queue> <db_url> <redis_url>",
            file=sys.stderr,
        )
        return 2
    run_id, queue, db_url, redis_url = argv

    # 必须在 import core.config **之前**设置：配置是 import 期常量。
    os.environ["AGENT_RUN_DISPATCH"] = "celery"
    os.environ["AGENT_RUN_QUEUE"] = queue
    os.environ["DATABASE_URL"] = db_url
    os.environ["REDIS_URL"] = redis_url
    os.environ["DEV_MODE"] = "true"
    os.environ["CELERY_BROKER_URL"] = redis_url
    os.environ.setdefault("LANGGRAPH_CHECKPOINT_BACKEND", "memory")

    # core.config 在 import 期执行 ``load_dotenv(override=True)``，会用仓库 .env 覆盖
    # 上面刚设的值（.env 默认 AGENT_RUN_DISPATCH=inline + SQLite）。这里像
    # tests/integration/celery_worker_runner.py 一样先把 dotenv 变成 no-op，让进程
    # 环境成为唯一事实源 —— 否则这个「API 进程」根本不是 celery 模式。
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]

    from runtime import dispatch

    task_id = asyncio.run(dispatch.dispatch_run(run_id))
    print(
        json.dumps(
            {
                "run_id": run_id,
                "task_id": task_id,
                "mode": dispatch.AGENT_RUN_DISPATCH,
                "queue": dispatch.AGENT_RUN_QUEUE,
                "pid": os.getpid(),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
