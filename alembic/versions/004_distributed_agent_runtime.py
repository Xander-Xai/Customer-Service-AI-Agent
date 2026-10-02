"""distributed agent runtime: agent_runs + agent_dead_letters + tool_side_effects

异步 Agent Run 的业务状态真相源（PostgreSQL/SQLite）。Redis/Celery 仅负责调度；
Celery result backend 不是真相源。

- ``agent_runs``：Run 记录 + worker lease/heartbeat + retry 调度 + 幂等键；
- ``agent_dead_letters``：application-level DLQ（retry 用尽的可查询证据）；
- ``tool_side_effects``：写操作工具的幂等 ledger（DB 唯一约束）。

Revision ID: 004_distributed_agent_runtime
Revises: 8ea0ec90ba74
Create Date: 2026-10-02

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004_distributed_agent_runtime"
down_revision: Union[str, None] = "8ea0ec90ba74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="QUEUED"),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_type", sa.String(length=16), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_thread_id", "agent_runs", ["thread_id"])
    op.create_index("ix_agent_runs_session_id", "agent_runs", ["session_id"])
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_idempotency_key", "agent_runs", ["idempotency_key"], unique=True)
    op.create_index("ix_agent_runs_status_created", "agent_runs", ["status", "created_at"])
    op.create_index("ix_agent_runs_thread", "agent_runs", ["thread_id", "created_at"])
    op.create_index("ix_agent_runs_status_next_retry", "agent_runs", ["status", "next_retry_at"])

    op.create_table(
        "agent_dead_letters",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("error_type", sa.String(length=16), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_dead_letters_run_id", "agent_dead_letters", ["run_id"], unique=True)
    op.create_index("ix_agent_dead_letters_thread_id", "agent_dead_letters", ["thread_id"])

    op.create_table(
        "tool_side_effects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("operation_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("result_reference", sa.JSON(), nullable=True),
        sa.Column("error_type", sa.String(length=16), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tool_name", "operation_key", name="uq_tool_side_effects_operation"),
    )
    op.create_index("ix_tool_side_effects_run_id", "tool_side_effects", ["run_id"])
    op.create_index("ix_tool_side_effects_thread_id", "tool_side_effects", ["thread_id"])
    op.create_index("ix_tool_side_effects_tool_name", "tool_side_effects", ["tool_name"])
    op.create_index("ix_tool_side_effects_status", "tool_side_effects", ["status"])
    op.create_index(
        "ix_tool_side_effects_tool_op", "tool_side_effects", ["tool_name", "operation_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_tool_side_effects_tool_op", table_name="tool_side_effects")
    op.drop_index("ix_tool_side_effects_status", table_name="tool_side_effects")
    op.drop_index("ix_tool_side_effects_tool_name", table_name="tool_side_effects")
    op.drop_index("ix_tool_side_effects_thread_id", table_name="tool_side_effects")
    op.drop_index("ix_tool_side_effects_run_id", table_name="tool_side_effects")
    op.drop_table("tool_side_effects")

    op.drop_index("ix_agent_dead_letters_thread_id", table_name="agent_dead_letters")
    op.drop_index("ix_agent_dead_letters_run_id", table_name="agent_dead_letters")
    op.drop_table("agent_dead_letters")

    op.drop_index("ix_agent_runs_status_next_retry", table_name="agent_runs")
    op.drop_index("ix_agent_runs_thread", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status_created", table_name="agent_runs")
    op.drop_index("ix_agent_runs_idempotency_key", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_session_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_thread_id", table_name="agent_runs")
    op.drop_table("agent_runs")
