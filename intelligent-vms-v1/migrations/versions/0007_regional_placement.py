"""regional node registry and placement

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "infrastructure_nodes",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("region_id", sa.String(length=128), nullable=False),
        sa.Column("roles_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("endpoints_json", sa.JSON(), nullable=False),
        sa.Column("capacity_json", sa.JSON(), nullable=False),
        sa.Column("load_json", sa.JSON(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_infrastructure_nodes_region_id", "infrastructure_nodes", ["region_id"])
    op.create_index("ix_infrastructure_nodes_state", "infrastructure_nodes", ["state"])
    op.create_index("ix_infrastructure_nodes_enabled", "infrastructure_nodes", ["enabled"])
    op.create_index("ix_infrastructure_nodes_heartbeat_at", "infrastructure_nodes", ["heartbeat_at"])

    op.create_table(
        "site_regions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("site_id", sa.String(length=128), nullable=False),
        sa.Column("region_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "site_id", name="uq_site_regions_tenant_site"),
    )
    op.create_index("ix_site_regions_tenant_id", "site_regions", ["tenant_id"])
    op.create_index("ix_site_regions_site_id", "site_regions", ["site_id"])
    op.create_index("ix_site_regions_region_id", "site_regions", ["region_id"])

    op.create_table(
        "placement_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("region_id", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reason", sa.String(length=128), nullable=False, server_default="initial"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["infrastructure_nodes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("camera_id", "role", name="uq_placement_camera_role"),
    )
    op.create_index("ix_placement_assignments_camera_id", "placement_assignments", ["camera_id"])
    op.create_index("ix_placement_assignments_role", "placement_assignments", ["role"])
    op.create_index("ix_placement_assignments_region_id", "placement_assignments", ["region_id"])
    op.create_index("ix_placement_assignments_node_id", "placement_assignments", ["node_id"])
    op.create_index("ix_placement_assignments_active", "placement_assignments", ["active"])
    op.create_index("ix_placement_assignments_lease_expires_at", "placement_assignments", ["lease_expires_at"])


def downgrade() -> None:
    for name in ("lease_expires_at","active","node_id","region_id","role","camera_id"):
        op.drop_index(f"ix_placement_assignments_{name}", table_name="placement_assignments")
    op.drop_table("placement_assignments")
    for name in ("region_id","site_id","tenant_id"):
        op.drop_index(f"ix_site_regions_{name}", table_name="site_regions")
    op.drop_table("site_regions")
    for name in ("heartbeat_at","enabled","state","region_id"):
        op.drop_index(f"ix_infrastructure_nodes_{name}", table_name="infrastructure_nodes")
    op.drop_table("infrastructure_nodes")
