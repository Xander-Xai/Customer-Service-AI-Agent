"""跨 worker（跨进程）checkpoint 持久化恢复测试（真实 PostgreSQL）。

仅在提供 TEST_POSTGRES_CHECKPOINT_URL 时运行：
    TEST_POSTGRES_CHECKPOINT_URL=postgresql://user:pass@host:5432/db \\
        pytest tests/integration/distributed_runtime/test_cross_worker_postgres.py -q

证明：worker1 崩溃后，worker2（全新 saver + 连接池）能从 PostgreSQL checkpoint
恢复并完成，已完成节点不重跑。
"""

import os
import uuid

import pytest

from runtime.errors import TransientError

from .harness import CheckpointedRuntime

CHECKPOINT_URL = os.getenv("TEST_POSTGRES_CHECKPOINT_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not CHECKPOINT_URL,
    reason="TEST_POSTGRES_CHECKPOINT_URL 未设置；需要真实 PostgreSQL 才能运行",
)


@pytest.mark.integration
async def test_checkpoint_survives_worker_process_restart():
    from core.checkpointer import build_postgres_checkpointer, close_checkpoint_runtime

    thread_id = f"xw-{uuid.uuid4().hex}"
    node_log: list[str] = []
    crash_flag = {"crash": True}

    worker1 = await build_postgres_checkpointer(CHECKPOINT_URL, setup_timeout=10.0)
    try:
        runtime1 = CheckpointedRuntime(
            worker1.checkpointer, node_log=node_log, crash_flag=crash_flag
        )
        with pytest.raises(TransientError):
            await runtime1.run(thread_id=thread_id, query="q")
    finally:
        await close_checkpoint_runtime(worker1)

    # 新 worker：全新 saver + 连接池，仅共享 PostgreSQL
    worker2 = await build_postgres_checkpointer(CHECKPOINT_URL, setup_timeout=10.0)
    try:
        runtime2 = CheckpointedRuntime(
            worker2.checkpointer, node_log=node_log, crash_flag=crash_flag
        )
        result = await runtime2.run(thread_id=thread_id, query="q")
        assert "node3" in result["nodes"]
        assert node_log.count("node1") == 1  # 已完成节点不重跑
    finally:
        await close_checkpoint_runtime(worker2)
