"""bounded regional autonomy state

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "placement_assignments",
        sa.Column("autonomy_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_placement_assignments_autonomy_expires_at",
        "placement_assignments",
        ["autonomy_expires_at"],
        unique=False,
    )
    op.add_column(
        "placement_revocations",
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_placement_revocations_valid_until",
        "placement_revocations",
        ["valid_until"],
        unique=False,
    )
    op.add_column(
        "infrastructure_nodes",
        sa.Column(
            "authority_mode",
            sa.String(length=32),
            nullable=False,
            server_default="central_online",
        ),
    )


def downgrade() -> None:
    op.drop_column("infrastructure_nodes", "authority_mode")
    op.drop_index(
        "ix_placement_revocations_valid_until",
        table_name="placement_revocations",
    )
    op.drop_column("placement_revocations", "valid_until")
    op.drop_index(
        "ix_placement_assignments_autonomy_expires_at",
        table_name="placement_assignments",
    )
    op.drop_column("placement_assignments", "autonomy_expires_at")
