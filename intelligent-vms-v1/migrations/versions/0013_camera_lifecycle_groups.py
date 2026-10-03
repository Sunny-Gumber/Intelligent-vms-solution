"""camera lifecycle metadata and groups

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add tenant/site camera groups and camera location/group metadata."""
    op.create_table(
        "camera_groups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("site_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
        sa.UniqueConstraint(
            "tenant_id",
            "site_id",
            "name",
            name="uq_camera_group_tenant_site_name",
        ),
    )
    op.create_index(
        "ix_camera_groups_tenant_id",
        "camera_groups",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_camera_groups_site_id",
        "camera_groups",
        ["site_id"],
        unique=False,
    )

    with op.batch_alter_table("cameras") as batch_op:
        batch_op.add_column(sa.Column("location_description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("group_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_cameras_group_id_camera_groups",
            "camera_groups",
            ["group_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_cameras_group_id", ["group_id"], unique=False)


def downgrade() -> None:
    """Remove camera group/location metadata."""
    with op.batch_alter_table("cameras") as batch_op:
        batch_op.drop_index("ix_cameras_group_id")
        batch_op.drop_constraint(
            "fk_cameras_group_id_camera_groups",
            type_="foreignkey",
        )
        batch_op.drop_column("group_id")
        batch_op.drop_column("location_description")

    op.drop_index("ix_camera_groups_site_id", table_name="camera_groups")
    op.drop_index("ix_camera_groups_tenant_id", table_name="camera_groups")
    op.drop_table("camera_groups")
