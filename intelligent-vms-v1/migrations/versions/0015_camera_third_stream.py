"""add third camera media path

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add optional third-stream path/key columns to managed cameras."""
    with op.batch_alter_table("cameras") as batch_op:
        batch_op.add_column(sa.Column("third_path", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("third_stream_key", sa.String(length=200), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_cameras_third_stream_key",
            ["third_stream_key"],
        )
        batch_op.create_index(
            "ix_cameras_third_stream_key",
            ["third_stream_key"],
            unique=False,
        )


def downgrade() -> None:
    """Remove optional third-stream path/key columns."""
    with op.batch_alter_table("cameras") as batch_op:
        batch_op.drop_index("ix_cameras_third_stream_key")
        batch_op.drop_constraint(
            "uq_cameras_third_stream_key",
            type_="unique",
        )
        batch_op.drop_column("third_stream_key")
        batch_op.drop_column("third_path")
