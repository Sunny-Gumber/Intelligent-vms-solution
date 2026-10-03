"""transactional event outbox

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.String(length=180), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("key_text", sa.String(length=512), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_outbox_topic", "event_outbox", ["topic"], unique=False)
    op.create_index("ix_event_outbox_status", "event_outbox", ["status"], unique=False)
    op.create_index("ix_event_outbox_next_attempt_at", "event_outbox", ["next_attempt_at"], unique=False)
    op.create_index("ix_event_outbox_claim_token", "event_outbox", ["claim_token"], unique=False)
    op.create_index("ix_event_outbox_claim_until", "event_outbox", ["claim_until"], unique=False)
    op.create_index("ix_event_outbox_delivered_at", "event_outbox", ["delivered_at"], unique=False)
    op.create_index(
        "ix_event_outbox_dispatch",
        "event_outbox",
        ["status", "next_attempt_at", "claim_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_event_outbox_dispatch", table_name="event_outbox")
    op.drop_index("ix_event_outbox_delivered_at", table_name="event_outbox")
    op.drop_index("ix_event_outbox_claim_until", table_name="event_outbox")
    op.drop_index("ix_event_outbox_claim_token", table_name="event_outbox")
    op.drop_index("ix_event_outbox_next_attempt_at", table_name="event_outbox")
    op.drop_index("ix_event_outbox_status", table_name="event_outbox")
    op.drop_index("ix_event_outbox_topic", table_name="event_outbox")
    op.drop_table("event_outbox")
