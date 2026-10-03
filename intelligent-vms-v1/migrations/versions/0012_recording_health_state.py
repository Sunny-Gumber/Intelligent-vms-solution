"""recording health state

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create durable per-camera recording completion health state."""
    op.create_table(
        "recording_health_state",
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column("last_segment_id", sa.String(length=64), nullable=True),
        sa.Column("last_segment_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gap_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recording_node_id", sa.String(length=128), nullable=True),
        sa.Column("assignment_generation", sa.Integer(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("camera_id"),
    )
    op.create_index(
        "ix_recording_health_last_segment_completed_at",
        "recording_health_state",
        ["last_segment_completed_at"],
        unique=False,
    )
    op.create_index(
        "ix_recording_health_gap_deadline_at",
        "recording_health_state",
        ["gap_deadline_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove durable per-camera recording completion health state."""
    op.drop_index("ix_recording_health_gap_deadline_at", table_name="recording_health_state")
    op.drop_index(
        "ix_recording_health_last_segment_completed_at",
        table_name="recording_health_state",
    )
    op.drop_table("recording_health_state")
