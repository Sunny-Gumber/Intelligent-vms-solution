"""Add nullable infrastructure-node role readiness.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-09

The column is additive. Upgrade does not drop a table, column, or index.
Downgrade does not drop it either: removing the column would discard explicit
role state, and a later upgrade would fail if the column were still present
after a version-only rollback.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, Sequence[str], None] = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add role_readiness_json when the column is not already present."""
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("infrastructure_nodes")}
    if "role_readiness_json" in columns:
        return
    with op.batch_alter_table("infrastructure_nodes") as batch:
        batch.add_column(sa.Column("role_readiness_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Retain role_readiness_json.

    This revision does not drop the column or any other schema object.
    """
    return None
