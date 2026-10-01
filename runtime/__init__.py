"""分布式 Agent Runtime（异步 Run + Celery worker）。

- AgentRun 业务状态真相源：PostgreSQL ``agent_runs`` 表。
- Redis/Celery 仅负责调度；Celery result backend 不是真相源。
- AgentRun != LangGraph Thread != Celery Task Result != Session。
"""

from .run_service import RunService, get_run_service
from .statuses import InvalidRunTransition, RunStatus

__all__ = [
    "RunService",
    "get_run_service",
    "RunStatus",
    "InvalidRunTransition",
]
