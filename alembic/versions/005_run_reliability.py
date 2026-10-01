"""run reliability: agent_runs lease/retry fields + tool_side_effects

- agent_runs: worker_id / lease_expires_at / heartbeat_at / error_type /
  last_error / next_retry_at（worker ownership + retry 调度）
- tool_side_effects: 写操作工具的幂等记录（唯一约束在 DB 层）

Revision ID: 005_run_reliability
Revises: 004_add_agent_runs
Create Date: 2026-10-02

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005_run_reliability"
down_revision: Union[str, None] = "004_add_agent_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("worker_id", sa.String(length=64), nullable=True))
    op.add_column(
        "agent_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "agent_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("agent_runs", sa.Column("error_type", sa.String(length=16), nullable=True))
    op.add_column("agent_runs", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "agent_runs", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_agent_runs_status_next_retry", "agent_runs", ["status", "next_retry_at"]
    )

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
        sa.UniqueConstraint(
            "tool_name", "operation_key", name="uq_tool_side_effects_operation"
        ),
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

    op.drop_index("ix_agent_runs_status_next_retry", table_name="agent_runs")
    op.drop_column("agent_runs", "next_retry_at")
    op.drop_column("agent_runs", "last_error")
    op.drop_column("agent_runs", "error_type")
    op.drop_column("agent_runs", "heartbeat_at")
    op.drop_column("agent_runs", "lease_expires_at")
    op.drop_column("agent_runs", "worker_id")
