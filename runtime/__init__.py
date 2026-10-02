"""分布式 Agent Runtime（异步 Run + Celery worker）。

- AgentRun 业务状态真相源：数据库 ``agent_runs`` 表。
- Redis/Celery 仅负责调度；Celery result backend 不是真相源。
- 四个 ID/概念严格区分：
    ``thread_id``（对话级，== session_id == LangGraph thread）
    ``run_id``（单轮 Graph 执行，每次请求唯一）
    ``task_id``（队列消息 / Worker 执行 ID）
    ``AgentRun``（业务运行记录，PostgreSQL canonical）
"""

from .run_service import RunService, get_run_service
from .statuses import InvalidRunTransition, RunStatus

__all__ = [
    "RunService",
    "get_run_service",
    "RunStatus",
    "InvalidRunTransition",
]
