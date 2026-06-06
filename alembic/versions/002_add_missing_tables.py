"""新增表 + 字段 — feedbacks, prompt_versions, users.force_password_change

P0-2: users 新增 force_password_change 字段
P0-4: 补齐 feedbacks 和 prompt_versions 表

Revision ID: 002_add_missing_tables
Revises: 001_initial_schema
Create Date: 2026-06-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "002_add_missing_tables"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===== P0-2: users 表新增 force_password_change 字段 =====
    op.add_column(
        "users",
        sa.Column("force_password_change", sa.Boolean(), server_default="0", nullable=True),
    )

    # ===== P0-4: feedbacks 表 =====
    op.create_table(
        "feedbacks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("message_index", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedbacks_session_id", "feedbacks", ["session_id"])

    # ===== P0-4: prompt_versions 表 =====
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Integer(), server_default="0"),
        sa.Column("score_avg", sa.Float(), server_default="0.0"),
        sa.Column("feedback_count", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_prompt_versions_agent_name", "prompt_versions", ["agent_name"])


def downgrade() -> None:
    op.drop_index("ix_prompt_versions_agent_name", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_index("ix_feedbacks_session_id", table_name="feedbacks")
    op.drop_table("feedbacks")
    op.drop_column("users", "force_password_change")
