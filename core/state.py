"""AgentState - 单一定义点（v4.4: 消除多处重复定义；v5.1: 多模态扩展）"""

from typing import TypedDict


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
    # v6.4: 业务结果契约（core.outcome.Outcome）——区分"交付"与"解决"。
    # degraded 为真表示 LLM/工具/检索发生降级，交付的可能是兜底文案。
    degraded: bool
    outcome: object  # core.outcome.Outcome（避免 core.state -> core.outcome 循环）
    trace_id: str
    stream_callback: object  # v4.2: SSE 流式回调
    # v5.1: 多模态扩展
    multimodal_content: (
        list  # OpenAI 多模态消息内容列表 [{"type":"text",...}, {"type":"image_url",...}]
    )
    has_multimodal: bool  # 是否包含多模态内容（用于快速判断）
    # P0-04: 认证用户身份 — 由 _run_graph 在可信请求边界写入，供下游
    # Cache / Quota / Tool AuthZ / Audit 消费。所有 transport 必须一致传递。
    user_id: str | None
    # human-in-the-loop 审批闸门（未发布版本；runtime 版本仍为 6.3）。
    # ``pending_actions``：被判定为 HIGH 风险、已从工具循环**摘出**（尚未执行）
    # 的副作用动作，交由 ``human_approval_gate`` 节点逐个 interrupt 等人工决策。
    # ``approval_results``：闸门恢复后每条动作的执行/拒绝结果。
    pending_actions: list[dict]
    approval_results: list[dict]
