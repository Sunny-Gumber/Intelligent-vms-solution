"""durable placement revocations for node fencing

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "placement_assignments",
        sa.Column("applied_generation", sa.Integer(), nullable=True),
    )
    op.create_table(
        "placement_revocations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("assignment_id", sa.String(length=36), nullable=False),
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("revoked_generation", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False, server_default="ownership_changed"),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["assignment_id"], ["placement_assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["infrastructure_nodes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assignment_id",
            "node_id",
            "revoked_generation",
            name="uq_placement_revocation_assignment_node_generation",
        ),
    )
    for name in ("assignment_id", "camera_id", "role", "node_id", "acknowledged_at", "cancelled_at"):
        op.create_index(
            f"ix_placement_revocations_{name}",
            "placement_revocations",
            [name],
            unique=False,
        )


def downgrade() -> None:
    for name in ("cancelled_at", "acknowledged_at", "node_id", "role", "camera_id", "assignment_id"):
        op.drop_index(
            f"ix_placement_revocations_{name}",
            table_name="placement_revocations",
        )
    op.drop_table("placement_revocations")
    op.drop_column("placement_assignments", "applied_generation")
