"""Constrain camera source protocol; backfill existing sources as RTSP."""

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add non-null protocol with durable legacy default and allowed-value constraint."""
    with op.batch_alter_table("cameras") as batch:
        batch.add_column(sa.Column("source_protocol", sa.String(5), nullable=False, server_default="rtsp"))
        batch.add_column(sa.Column("source_fingerprint", sa.String(64), nullable=True))
        batch.create_check_constraint("ck_camera_source_protocol", "source_protocol IN ('rtsp', 'rtsps')")
        batch.create_check_constraint("ck_camera_source_fingerprint", "source_fingerprint IS NULL OR (source_protocol = 'rtsps' AND length(source_fingerprint) = 64)")


def downgrade() -> None:
    """Remove protocol only when no secure source would be silently downgraded."""
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM cameras WHERE source_protocol = 'rtsps'")).scalar():
        raise RuntimeError("RTSPS cameras require explicit reconfiguration before schema downgrade")
    with op.batch_alter_table("cameras") as batch:
        batch.drop_constraint("ck_camera_source_fingerprint", type_="check")
        batch.drop_constraint("ck_camera_source_protocol", type_="check")
        batch.drop_column("source_fingerprint")
        batch.drop_column("source_protocol")
