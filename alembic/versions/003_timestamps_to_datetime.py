"""Float 时间戳 → DateTime(timezone=True)

将所有 Float 时间戳字段迁移为 DateTime，支持时区感知。

Revision ID: 003_timestamps_to_datetime
Revises: 002_add_missing_tables
Create Date: 2026-06-06

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "003_timestamps_to_datetime"
down_revision: Union[str, None] = "002_add_missing_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """将 Float 时间戳转换为 DateTime（timezone=True）"""

    # ── users 表 ──
    op.alter_column(
        "users",
        "created_at",
        type_=sa.DateTime(timezone=True),
        existing_nullable=False,
        postgresql_using="TO_TIMESTAMP(created_at)",
    )
    op.alter_column(
        "users",
        "last_login_at",
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        postgresql_using="TO_TIMESTAMP(last_login_at)",
    )

    # ── chat_histories 表 ──
    op.alter_column(
        "chat_histories",
        "created_at",
        type_=sa.DateTime(timezone=True),
        existing_nullable=False,
        postgresql_using="TO_TIMESTAMP(created_at)",
    )
    op.alter_column(
        "chat_histories",
        "updated_at",
        type_=sa.DateTime(timezone=True),
        existing_nullable=False,
        postgresql_using="TO_TIMESTAMP(updated_at)",
    )

    # ── audit_logs 表 ──
    op.alter_column(
        "audit_logs",
        "timestamp",
        type_=sa.DateTime(timezone=True),
        existing_nullable=False,
        postgresql_using="TO_TIMESTAMP(timestamp)",
    )

    # ── feedbacks 表 ──
    op.alter_column(
        "feedbacks",
        "created_at",
        type_=sa.DateTime(timezone=True),
        existing_nullable=False,
        postgresql_using="TO_TIMESTAMP(created_at)",
    )

    # ── prompt_versions 表 ──
    op.alter_column(
        "prompt_versions",
        "created_at",
        type_=sa.DateTime(timezone=True),
        existing_nullable=False,
        postgresql_using="TO_TIMESTAMP(created_at)",
    )


def downgrade() -> None:
    """回退：DateTime → Float"""
    for table, column in [
        ("users", "created_at"),
        ("users", "last_login_at"),
        ("chat_histories", "created_at"),
        ("chat_histories", "updated_at"),
        ("audit_logs", "timestamp"),
        ("feedbacks", "created_at"),
        ("prompt_versions", "created_at"),
    ]:
        op.alter_column(
            table,
            column,
            type_=sa.Float(),
            existing_nullable=False,
            postgresql_using=f"EXTRACT(EPOCH FROM {column})",
        )
