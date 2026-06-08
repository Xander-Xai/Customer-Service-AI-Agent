"""AgentState - 单一定义点（v4.4: 消除多处重复定义）"""
from typing import List, TypedDict


class AgentState(TypedDict, total=False):
    session_id: str
    current_agent: str
    customer_query: str
    query_type: str
    response: str
    complexity: int
    fast_path: bool
    collaboration_mode: str
    cached: bool
    agents_used: List[str]
    resolution_status: str  # resolved | uncertain | failed | escalated
    trace_id: str
    stream_callback: object  # v4.2: SSE 流式回调
