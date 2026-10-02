"""tool_side_effects: 增加认领所有权列（原子互斥 claim）

Revision ID: 005_tool_side_effect_claim_owner
Revises: 004_distributed_agent_runtime

为什么需要（来自 PR review）：
    `SideEffectStore.claim()` 过去是"SELECT -> 判断 -> UPDATE"三步。两个并发 worker
    可以同时读到同一条 PENDING 行、同时判定"可以执行"、同时拿到 CLAIM_EXECUTE，
    于是同一笔退款/改单被执行两次——唯一约束只防重复行，防不住重复执行。
    另外 `mark_succeeded` / `mark_failed` 没有归属校验，两个执行者的结果会互相覆盖。

本迁移加入：
    claim_owner        —— 当前认领者的唯一 token（host:pid:uuid）
    claim_expires_at   —— 认领租约的显式截止时间（不再依赖 updated_at 推断）

唯一约束 `uq_tool_side_effects_operation` 保持不变。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "005_tool_side_effect_claim_owner"
down_revision: str = "004_distributed_agent_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tool_side_effects" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("tool_side_effects")}
    if "claim_owner" not in existing:
        op.add_column(
            "tool_side_effects",
            sa.Column("claim_owner", sa.String(length=64), nullable=True),
        )
    if "claim_expires_at" not in existing:
        op.add_column(
            "tool_side_effects",
            sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tool_side_effects" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("tool_side_effects")}
    if "claim_expires_at" in existing:
        op.drop_column("tool_side_effects", "claim_expires_at")
    if "claim_owner" in existing:
        op.drop_column("tool_side_effects", "claim_owner")
