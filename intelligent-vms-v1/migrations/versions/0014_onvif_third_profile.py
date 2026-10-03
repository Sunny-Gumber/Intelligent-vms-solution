"""add third managed ONVIF profile token

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add optional third managed profile token to ONVIF capability snapshots."""
    with op.batch_alter_table("camera_capabilities") as batch_op:
        batch_op.add_column(
            sa.Column("third_profile_token", sa.String(length=255), nullable=True)
        )


def downgrade() -> None:
    """Remove optional third managed profile token."""
    with op.batch_alter_table("camera_capabilities") as batch_op:
        batch_op.drop_column("third_profile_token")
