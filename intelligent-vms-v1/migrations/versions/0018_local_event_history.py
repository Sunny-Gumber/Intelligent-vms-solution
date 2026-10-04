"""local normalized event history for reduced profiles

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0018"
down_revision: Union[str, Sequence[str], None] = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_history",
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("site_id", sa.String(length=128), nullable=False),
        sa.Column("camera_id", sa.String(length=128), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("object_type", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("zone_id", sa.String(length=128), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("snapshot_uri", sa.Text(), nullable=True),
        sa.Column("recording_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recording_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attributes_json", sa.JSON(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    for name in ("tenant_id","site_id","camera_id","timestamp","event_type","severity","ingested_at"):
        op.create_index(f"ix_event_history_{name}", "event_history", [name], unique=False)
    op.create_index(
        "ix_event_history_scope_time",
        "event_history",
        ["tenant_id", "site_id", "timestamp", "event_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_event_history_scope_time", table_name="event_history")
    for name in ("ingested_at","severity","event_type","timestamp","camera_id","site_id","tenant_id"):
        op.drop_index(f"ix_event_history_{name}", table_name="event_history")
    op.drop_table("event_history")
