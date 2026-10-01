"""
SQLAlchemy 数据模型（v4.3 — 生产加固版）
User / ChatHistory / AuditLog / Feedback / PromptVersion

v4.3 变更：
- 时间戳从 Float 迁移为 DateTime(timezone=True)，支持时区感知
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _utcnow():
    """返回当前 UTC 时间"""
    return datetime.now(timezone.utc)


class User(Base):
    """用户表"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(
        String(20), nullable=False, default="customer"
    )  # customer | agent | supervisor | admin
    display_name = Column(String(128), default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Integer, default=1)  # 0=禁用 1=启用
    force_password_change = Column(Boolean, default=False)  # P0-2: 管理员首次登录强制改密

    chat_histories = relationship("ChatHistory", back_populates="user", lazy="dynamic")
    audit_logs = relationship("AuditLog", back_populates="user", lazy="dynamic")


class ChatHistory(Base):
    """会话历史持久化"""

    __tablename__ = "chat_histories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    session_id = Column(String(64), nullable=False, index=True)
    messages = Column(JSON, nullable=False, default=list)
    summary = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    user = relationship("User", back_populates="chat_histories")

    # v5.1: 复合索引，加速按 session 查询历史（按时间排序）
    # v5.4: 新增用户维度复合索引，加速用户历史查询
    __table_args__ = (
        Index("ix_chat_session_created", "session_id", "created_at"),
        Index("ix_chat_user_created", "user_id", "created_at"),  # P0: 优化用户历史查询
    )


class AuditLog(Base):
    """操作审计日志"""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(
        String(64), nullable=False, index=True
    )  # login / register / query / admin_action
    detail = Column(Text, default="")
    ip_address = Column(String(64), default="")
    timestamp = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    user = relationship("User", back_populates="audit_logs")

    # v5.4: 复合索引，加速审计日志查询（按动作和时间筛选）
    __table_args__ = (
        Index("ix_audit_action_time", "action", "timestamp"),  # P0: 优化审计查询
    )


class Feedback(Base):
    """用户反馈（点赞/点踩）"""

    __tablename__ = "feedbacks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), nullable=False, index=True)
    message_index = Column(Integer, nullable=False)  # 第几条回复
    rating = Column(Integer, nullable=False)  # 1=点赞, -1=点踩
    comment = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class PromptVersion(Base):
    """Prompt 版本管理（v4.1）
    用于 A/B 测试框架中追踪不同 Prompt 策略的效果。
    """

    __tablename__ = "prompt_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_name = Column(String(64), nullable=False, index=True)
    version = Column(String(20), nullable=False)
    prompt_text = Column(Text, nullable=False)
    is_active = Column(Integer, default=0)  # 0=未启用, 1=启用
    score_avg = Column(Float, default=0.0)  # 平均评分
    feedback_count = Column(Integer, default=0)  # 反馈计数
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class AgentRun(Base):
    """Agent 运行记录（异步分布式执行）。

    业务状态真相源（PostgreSQL）；Redis/Celery 仅负责调度，Celery result backend
    不是真相源。AgentRun 与 LangGraph Thread（checkpoint）、Session、Celery Task
    Result 是四个不同概念，详见 docs/design/distributed-agent-runtime.md。

    合法状态迁移由 runtime/statuses.py 定义并在 runtime/run_service.py 强制。
    """

    __tablename__ = "agent_runs"

    id = Column(String(36), primary_key=True)  # UUID 字符串
    thread_id = Column(String(64), nullable=False, index=True)  # == LangGraph thread_id
    session_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=True, index=True)
    status = Column(String(16), nullable=False, default="PENDING", index=True)
    query = Column(Text, nullable=False)
    result = Column(JSON, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    attempt = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    queued_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    trace_id = Column(String(64), nullable=True)
    # 幂等键：同一 key 只创建一个 run（可为空；空值允许多条）
    idempotency_key = Column(String(128), nullable=True, unique=True, index=True)
    # Run 可靠性（worker ownership / lease / heartbeat）
    worker_id = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    # 错误分类与重试调度
    error_type = Column(String(16), nullable=True)  # transient|permanent|cancelled|timeout
    last_error = Column(Text, nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_agent_runs_status_created", "status", "created_at"),
        Index("ix_agent_runs_thread", "thread_id", "created_at"),
        Index("ix_agent_runs_status_next_retry", "status", "next_retry_at"),
    )


class ToolSideEffect(Base):
    """写操作工具（退款/订单修改/工单等）的幂等记录。

    at-least-once delivery + application-level idempotency：
    worker 重投递/崩溃恢复时，同一 (tool_name, operation_key) 已 SUCCEEDED 的
    副作用不得再次执行。唯一约束在数据库层，不依赖 Redis。

    只读工具不写此表（避免无意义复杂度）。
    """

    __tablename__ = "tool_side_effects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), nullable=False, index=True)
    thread_id = Column(String(64), nullable=True, index=True)
    tool_name = Column(String(64), nullable=False, index=True)
    operation_key = Column(String(200), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="PENDING", index=True)
    result_reference = Column(JSON, nullable=True)
    error_type = Column(String(16), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tool_name", "operation_key", name="uq_tool_side_effects_operation"
        ),
        Index("ix_tool_side_effects_tool_op", "tool_name", "operation_key"),
    )


class HumanApproval(Base):
    """高风险操作的人工审批记录（durable）。

    只对 HIGH 风险 Tool（退款/改单/高额赔付/投诉升级/ERP 写操作）创建。
    审批决策是业务控制边界；approve 后仍经 Tool idempotency 防止 resume/retry
    重复执行。唯一约束 (run_id, action, proposal_fingerprint) 让 resume 重新
    interrupt 时幂等复用同一条审批，不产生重复。
    """

    __tablename__ = "human_approvals"

    approval_id = Column(String(36), primary_key=True)
    run_id = Column(String(36), nullable=False, index=True)
    thread_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=True, index=True)  # 请求人（不可自审）
    action = Column(String(64), nullable=False, index=True)
    risk_level = Column(String(16), nullable=False, default="high")
    agent = Column(String(64), nullable=True)
    proposal = Column(JSON, nullable=False, default=dict)  # 脱敏后的参数
    proposal_fingerprint = Column(String(64), nullable=False)
    status = Column(
        String(16), nullable=False, default="PENDING", index=True
    )  # PENDING | APPROVED | REJECTED | EXPIRED
    requested_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reviewer_id = Column(String(64), nullable=True)
    decision = Column(JSON, nullable=True)  # {decision, edited_args, reason}
    reason = Column(Text, nullable=True)
    resumed_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "action",
            "proposal_fingerprint",
            name="uq_human_approvals_proposal",
        ),
        Index("ix_human_approvals_status_requested", "status", "requested_at"),
    )
