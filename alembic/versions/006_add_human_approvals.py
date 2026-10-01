"""add human_approvals table (human-in-the-loop high-risk approval)

Revision ID: 006_add_human_approvals
Revises: 005_run_reliability
Create Date: 2026-10-02

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "006_add_human_approvals"
down_revision: Union[str, None] = "005_run_reliability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "human_approvals",
        sa.Column("approval_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False, server_default="high"),
        sa.Column("agent", sa.String(length=64), nullable=True),
        sa.Column("proposal", sa.JSON(), nullable=False),
        sa.Column("proposal_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_id", sa.String(length=64), nullable=True),
        sa.Column("decision", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint(
            "run_id", "action", "proposal_fingerprint", name="uq_human_approvals_proposal"
        ),
    )
    op.create_index("ix_human_approvals_run_id", "human_approvals", ["run_id"])
    op.create_index("ix_human_approvals_thread_id", "human_approvals", ["thread_id"])
    op.create_index("ix_human_approvals_user_id", "human_approvals", ["user_id"])
    op.create_index("ix_human_approvals_action", "human_approvals", ["action"])
    op.create_index("ix_human_approvals_status", "human_approvals", ["status"])
    op.create_index(
        "ix_human_approvals_status_requested", "human_approvals", ["status", "requested_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_human_approvals_status_requested", table_name="human_approvals")
    op.drop_index("ix_human_approvals_status", table_name="human_approvals")
    op.drop_index("ix_human_approvals_action", table_name="human_approvals")
    op.drop_index("ix_human_approvals_user_id", table_name="human_approvals")
    op.drop_index("ix_human_approvals_thread_id", table_name="human_approvals")
    op.drop_index("ix_human_approvals_run_id", table_name="human_approvals")
    op.drop_table("human_approvals")
