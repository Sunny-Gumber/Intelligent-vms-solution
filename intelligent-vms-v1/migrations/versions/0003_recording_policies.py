"""recording policies

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recording_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="disabled"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("record_stream_key", sa.String(length=200), nullable=False),
        sa.Column("recording_node_id", sa.String(length=128), nullable=False, server_default="media-local-01"),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("part_duration_ms", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("segment_duration_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("max_part_size_mb", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("camera_id", name="uq_recording_policy_camera_id"),
        sa.UniqueConstraint("record_stream_key", name="uq_recording_policy_stream_key"),
    )
    op.create_index("ix_recording_policies_camera_id", "recording_policies", ["camera_id"], unique=False)
    op.create_index("ix_recording_policies_record_stream_key", "recording_policies", ["record_stream_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_recording_policies_record_stream_key", table_name="recording_policies")
    op.drop_index("ix_recording_policies_camera_id", table_name="recording_policies")
    op.drop_table("recording_policies")
