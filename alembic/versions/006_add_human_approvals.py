"""human-in-the-loop: human_approvals (high-risk side-effect approval)

高风险工具副作用的人工审批凭证表。与 ``agent_runs`` / ``tool_side_effects``
同属 durable 业务真相（PostgreSQL/SQLite），不是 Redis/Celery 状态。

- ``human_approvals``：审批请求 + 决策 + 脱敏提案留痕；``PENDING ->
  APPROVED | REJECTED | EXPIRED``；
- 幂等基础：``uq_human_approvals_proposal`` (run_id, action,
  proposal_fingerprint)，同一 run 的同一动作 + 同一提案只会产生一条审批，
  因此 at-least-once 重投递不会重复打扰审批人；
- ``expires_at`` 支撑审批 TTL：超时未决策落 ``EXPIRED``（等同拒绝，绝不默认放行）。

Revision ID: 006_add_human_approvals
Revises: 005_tool_side_effect_claim_owner
Create Date: 2026-10-02

为什么 Revises 005 而不是 004
----------------------------
``005_tool_side_effect_claim_owner`` 为 ``tool_side_effects`` 加入了
``claim_owner`` / ``claim_expires_at``（PR #28 的原子互斥认领修复）。审批通过后的
副作用执行**复用**该 ledger（``operation_key = run_id:approval:{approval_id}``），
所以本迁移必须排在其后，不能与之并行分叉。

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006_add_human_approvals"
down_revision: Union[str, None] = "005_tool_side_effect_claim_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "human_approvals",
        sa.Column("approval_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=True),
        sa.Column("proposal", sa.JSON(), nullable=False),
        sa.Column("proposal_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_id", sa.String(length=64), nullable=True),
        sa.Column("decision", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("approval_id"),
    )
    op.create_index("ix_human_approvals_run_id", "human_approvals", ["run_id"])
    op.create_index("ix_human_approvals_thread_id", "human_approvals", ["thread_id"])
    op.create_index("ix_human_approvals_user_id", "human_approvals", ["user_id"])
    op.create_index("ix_human_approvals_action", "human_approvals", ["action"])
    op.create_index("ix_human_approvals_risk_level", "human_approvals", ["risk_level"])
    op.create_index("ix_human_approvals_status", "human_approvals", ["status"])
    op.create_index("ix_human_approvals_expires_at", "human_approvals", ["expires_at"])
    op.create_index(
        "ix_human_approvals_status_requested",
        "human_approvals",
        ["status", "requested_at"],
    )
    op.create_index("ix_human_approvals_run_status", "human_approvals", ["run_id", "status"])
    op.create_unique_constraint(
        "uq_human_approvals_proposal",
        "human_approvals",
        ["run_id", "action", "proposal_fingerprint"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_human_approvals_proposal", "human_approvals", type_="unique")
    op.drop_index("ix_human_approvals_run_status", table_name="human_approvals")
    op.drop_index("ix_human_approvals_status_requested", table_name="human_approvals")
    op.drop_index("ix_human_approvals_expires_at", table_name="human_approvals")
    op.drop_index("ix_human_approvals_status", table_name="human_approvals")
    op.drop_index("ix_human_approvals_risk_level", table_name="human_approvals")
    op.drop_index("ix_human_approvals_action", table_name="human_approvals")
    op.drop_index("ix_human_approvals_user_id", table_name="human_approvals")
    op.drop_index("ix_human_approvals_thread_id", table_name="human_approvals")
    op.drop_index("ix_human_approvals_run_id", table_name="human_approvals")
    op.drop_table("human_approvals")
