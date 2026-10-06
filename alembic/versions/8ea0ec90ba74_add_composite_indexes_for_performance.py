"""add_composite_indexes_for_performance

Revision ID: 8ea0ec90ba74
Revises: 003_timestamps_to_datetime
Create Date: 2026-06-16 16:45:42.055331

v5.4: 添加复合索引优化常用查询性能
- ix_chat_user_created: 加速用户历史查询
- ix_audit_action_time: 加速审计日志查询
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ea0ec90ba74"
down_revision: Union[str, None] = "003_timestamps_to_datetime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """添加性能优化索引"""
    # 为 chat_histories 表添加用户维度复合索引
    op.create_index("ix_chat_user_created", "chat_histories", ["user_id", "created_at"])

    # 为 audit_logs 表添加动作时间复合索引
    op.create_index("ix_audit_action_time", "audit_logs", ["action", "timestamp"])


def downgrade() -> None:
    """回滚索引"""
    op.drop_index("ix_audit_action_time", table_name="audit_logs")
    op.drop_index("ix_chat_user_created", table_name="chat_histories")
