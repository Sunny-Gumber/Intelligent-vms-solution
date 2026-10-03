"""add durable manual recording sessions

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create durable server-timed manual-recording interval intents."""
    op.create_table(
        "manual_recording_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("camera_id", sa.String(36), sa.ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True),
        sa.Column("operator_subject", sa.String(256), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_stop_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('ACTIVE','STOPPED')", name="ck_manual_recording_state"),
        sa.CheckConstraint("max_stop_at >= started_at", name="ck_manual_recording_max_after_start"),
        sa.CheckConstraint("stopped_at IS NULL OR (stopped_at >= started_at AND stopped_at <= max_stop_at)", name="ck_manual_recording_stop_bounds"),
    )
    for column in ("tenant_id","site_id","camera_id","operator_subject","state","started_at","max_stop_at"):
        op.create_index(f"ix_manual_recording_sessions_{column}", "manual_recording_sessions", [column])
    op.create_index(
        "uq_manual_recording_active_owner_camera",
        "manual_recording_sessions",
        ["tenant_id","site_id","camera_id","operator_subject"],
        unique=True,
        postgresql_where=sa.text("state = 'ACTIVE'"),
        sqlite_where=sa.text("state = 'ACTIVE'"),
    )


def downgrade() -> None:
    """Remove durable manual-recording interval intents."""
    op.drop_table("manual_recording_sessions")
