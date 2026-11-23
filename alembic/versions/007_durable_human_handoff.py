"""Durable customer-to-human handoff and append-only messages.

Revision ID: 007_durable_human_handoff
Revises: 006_add_human_approvals
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "007_durable_human_handoff"
down_revision: Union[str, None] = "006_add_human_approvals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "handoff_tickets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="OPEN"),
        sa.Column("assigned_to", sa.String(length=64), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_handoff_tickets_session_id", "handoff_tickets", ["session_id"], unique=True)
    op.create_index("ix_handoff_tickets_customer_id", "handoff_tickets", ["customer_id"])
    op.create_index("ix_handoff_tickets_status", "handoff_tickets", ["status"])
    op.create_index("ix_handoff_tickets_assigned_to", "handoff_tickets", ["assigned_to"])
    op.create_index("ix_handoff_queue", "handoff_tickets", ["status", "created_at"])
    op.create_index(
        "ix_handoff_customer_status", "handoff_tickets", ["customer_id", "status"]
    )

    op.create_table(
        "handoff_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("ticket_id", sa.String(length=36), sa.ForeignKey("handoff_tickets.id"), nullable=False),
        sa.Column("author_id", sa.String(length=64), nullable=False),
        sa.Column("sender_role", sa.String(length=16), nullable=False),
        sa.Column("client_message_id", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ticket_id", "author_id", "client_message_id", name="uq_handoff_message_delivery"
        ),
    )
    op.create_index("ix_handoff_messages_ticket_id", "handoff_messages", ["ticket_id"])
    op.create_index(
        "ix_handoff_messages_ticket_created", "handoff_messages", ["ticket_id", "created_at"]
    )

    op.create_table(
        "handoff_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("ticket_id", sa.String(length=36), sa.ForeignKey("handoff_tickets.id"), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_handoff_events_ticket_id", "handoff_events", ["ticket_id"])
    op.create_index(
        "ix_handoff_events_ticket_created", "handoff_events", ["ticket_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_handoff_events_ticket_created", table_name="handoff_events")
    op.drop_index("ix_handoff_events_ticket_id", table_name="handoff_events")
    op.drop_table("handoff_events")
    op.drop_index("ix_handoff_messages_ticket_created", table_name="handoff_messages")
    op.drop_index("ix_handoff_messages_ticket_id", table_name="handoff_messages")
    op.drop_table("handoff_messages")
    for idx in (
        "ix_handoff_customer_status",
        "ix_handoff_queue",
        "ix_handoff_tickets_assigned_to",
        "ix_handoff_tickets_status",
        "ix_handoff_tickets_customer_id",
        "ix_handoff_tickets_session_id",
    ):
        op.drop_index(idx, table_name="handoff_tickets")
    op.drop_table("handoff_tickets")
