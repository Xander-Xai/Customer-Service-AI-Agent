"""
SQLAlchemy 数据模型（v4.3 — 生产加固版）
User / ChatHistory / AuditLog / Feedback / PromptVersion

v4.3 变更：
- 时间戳从 Float 迁移为 DateTime(timezone=True)，支持时区感知
"""
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, ForeignKey, JSON, DateTime
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
    role = Column(String(20), nullable=False, default="user")  # admin | user
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


class AuditLog(Base):
    """操作审计日志"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(64), nullable=False, index=True)  # login / register / query / admin_action
    detail = Column(Text, default="")
    ip_address = Column(String(64), default="")
    timestamp = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    user = relationship("User", back_populates="audit_logs")


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
