"""persist placement revocation execution keys

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist exact execution keys owned by revoked placement generations."""
    with op.batch_alter_table("placement_revocations") as batch_op:
        batch_op.add_column(
            sa.Column("execution_keys_json", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    """Remove persisted placement-revocation execution keys."""
    with op.batch_alter_table("placement_revocations") as batch_op:
        batch_op.drop_column("execution_keys_json")
