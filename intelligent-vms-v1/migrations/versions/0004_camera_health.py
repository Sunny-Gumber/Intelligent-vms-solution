"""camera health state

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "camera_health_state",
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="unknown"),
        sa.Column("path_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("camera_id"),
    )
    op.create_index("ix_camera_health_state_state", "camera_health_state", ["state"], unique=False)
    op.create_index("ix_camera_health_state_observed_at", "camera_health_state", ["observed_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_camera_health_state_observed_at", table_name="camera_health_state")
    op.drop_index("ix_camera_health_state_state", table_name="camera_health_state")
    op.drop_table("camera_health_state")
