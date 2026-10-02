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

    业务状态真相源（PostgreSQL/SQLite ``agent_runs`` 表）；Redis/Celery 仅负责
    调度，Celery result backend 不是真相源。

    三个 ID 概念严格区分（详见 docs/design/distributed-agent-runtime.md）：
      - ``thread_id``：对话级 ID（== 业务 session_id == LangGraph thread_id），
        同一多轮会话持续复用；
      - ``id``（run_id）：单轮 Graph 执行 ID，每次用户请求唯一；
      - ``task_id``：队列消息 / Worker 执行 ID（Celery task id），异步执行后才存在。

    合法状态迁移由 ``runtime/statuses.py`` 定义并在 ``runtime/run_service.py``
    通过原子条件更新强制。
    """

    __tablename__ = "agent_runs"

    id = Column(String(36), primary_key=True)  # run_id (UUID 字符串)
    thread_id = Column(String(64), nullable=False, index=True)
    session_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=True, index=True)
    status = Column(String(16), nullable=False, default="QUEUED", index=True)
    query = Column(Text, nullable=False)
    result = Column(JSON, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    error_type = Column(String(16), nullable=True)  # transient|permanent|timeout
    last_error = Column(Text, nullable=True)
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
    # Worker ownership / lease / heartbeat
    worker_id = Column(String(64), nullable=True)
    task_id = Column(String(64), nullable=True)  # 最近一次消费的 Celery task id
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_agent_runs_status_created", "status", "created_at"),
        Index("ix_agent_runs_thread", "thread_id", "created_at"),
        Index("ix_agent_runs_status_next_retry", "status", "next_retry_at"),
    )


class AgentDeadLetter(Base):
    """应用级 Dead Letter 记录（retry 用尽后的可查询证据）。

    注意：这是 **application-level DLQ**，不是 broker-native DLX。Redis broker
    只负责重投递；把 run 归入死信由应用显式写入本表。可回答：
      - 哪个 run 失败？
      - 失败几次？
      - 最后错误是什么？
      - 什么时候进入 DLQ？
    """

    __tablename__ = "agent_dead_letters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), nullable=False, unique=True, index=True)
    thread_id = Column(String(64), nullable=False, index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    error_type = Column(String(16), nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    worker_id = Column(String(64), nullable=True)
    entered_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ToolSideEffect(Base):
    """写操作工具（退款/改单/工单等）的幂等 ledger。

    at-least-once delivery + application-level idempotency：同一
    ``(tool_name, operation_key)`` 一旦 SUCCEEDED，重投递 / 崩溃恢复不得再次
    执行。唯一约束在数据库层，不依赖 Redis。

    边界：该 ledger 只保证「同一 Agent 不会重复发起同一副作用」。若下游外部
    系统（ERP 等）需要真正的端到端幂等，必须由下游 API 接受 idempotency key，
    或由本 ledger 配合人工对账，不构成本 PR 的分布式事务保证。
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
    # 认领所有权：只有持有当前 claim_owner 的执行者才能把这次副作用标记为
    # 成功/失败。否则并发重试可能互相覆盖结果（两个 worker 都在跑同一笔写操作时，
    # 后完成的那个会盖掉先完成的结果）。
    claim_owner = Column(String(64), nullable=True)
    claim_expires_at = Column(DateTime(timezone=True), nullable=True)
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
