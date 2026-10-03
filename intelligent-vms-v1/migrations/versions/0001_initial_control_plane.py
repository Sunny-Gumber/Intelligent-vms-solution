"""initial VMS control-plane schema

Revision ID: 0001
Revises:
Create Date: 2026-09-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cameras",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("site_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("rtsp_port", sa.Integer(), nullable=False),
        sa.Column("main_path", sa.Text(), nullable=False),
        sa.Column("sub_path", sa.Text(), nullable=True),
        sa.Column("username_enc", sa.Text(), nullable=True),
        sa.Column("password_enc", sa.Text(), nullable=True),
        sa.Column("stream_key", sa.String(length=180), nullable=False),
        sa.Column("media_node_id", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("desired_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stream_key", name="uq_camera_stream_key"),
    )
    op.create_index("ix_cameras_site_id", "cameras", ["site_id"], unique=False)
    op.create_index("ix_cameras_stream_key", "cameras", ["stream_key"], unique=False)

    op.create_table(
        "camera_capabilities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column("onvif_xaddr", sa.Text(), nullable=False),
        sa.Column("device_info_json", sa.JSON(), nullable=False),
        sa.Column("services_json", sa.JSON(), nullable=False),
        sa.Column("features_json", sa.JSON(), nullable=False),
        sa.Column("profiles_json", sa.JSON(), nullable=False),
        sa.Column("main_profile_token", sa.String(length=255), nullable=True),
        sa.Column("sub_profile_token", sa.String(length=255), nullable=True),
        sa.Column("probed_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("probe_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("camera_id", name="uq_camera_capabilities_camera_id"),
    )
    op.create_index("ix_camera_capabilities_camera_id", "camera_capabilities", ["camera_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_camera_capabilities_camera_id", table_name="camera_capabilities")
    op.drop_table("camera_capabilities")
    op.drop_index("ix_cameras_stream_key", table_name="cameras")
    op.drop_index("ix_cameras_site_id", table_name="cameras")
    op.drop_table("cameras")
