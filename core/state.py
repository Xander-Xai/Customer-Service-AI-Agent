"""AgentState - 单一定义点（v4.4: 消除多处重复定义；v5.1: 多模态扩展）"""

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
    agents_used: list[str]
    resolution_status: str  # resolved | uncertain | failed | escalated
    trace_id: str
    stream_callback: object  # v4.2: SSE 流式回调
    # v5.1: 多模态扩展
    multimodal_content: (
        list  # OpenAI 多模态消息内容列表 [{"type":"text",...}, {"type":"image_url",...}]
    )
    has_multimodal: bool  # 是否包含多模态内容（用于快速判断）
