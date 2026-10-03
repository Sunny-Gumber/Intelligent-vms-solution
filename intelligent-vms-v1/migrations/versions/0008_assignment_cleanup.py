"""track placement nodes requiring failover cleanup

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-26
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "placement_assignments",
        sa.Column(
            "cleanup_node_ids_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("placement_assignments", "cleanup_node_ids_json")
