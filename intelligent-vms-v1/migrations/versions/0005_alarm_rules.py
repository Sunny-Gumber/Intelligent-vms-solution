"""alarm rules and instances

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alarm_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("site_id", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("event_types_json", sa.JSON(), nullable=False),
        sa.Column("severities_json", sa.JSON(), nullable=False),
        sa.Column("camera_ids_json", sa.JSON(), nullable=False),
        sa.Column("alarm_severity", sa.String(length=16), nullable=False, server_default="high"),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alarm_rules_tenant_id", "alarm_rules", ["tenant_id"], unique=False)
    op.create_index("ix_alarm_rules_site_id", "alarm_rules", ["site_id"], unique=False)
    op.create_index("ix_alarm_rules_enabled", "alarm_rules", ["enabled"], unique=False)

    op.create_table(
        "alarm_instances",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("rule_id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("site_id", sa.String(length=128), nullable=False),
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="open"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=256), nullable=True),
        sa.ForeignKeyConstraint(["rule_id"], ["alarm_rules.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_alarm_instances_dedupe_key"),
    )
    for name in ("rule_id","event_id","tenant_id","site_id","camera_id","event_type","severity","state","opened_at"):
        op.create_index(f"ix_alarm_instances_{name}", "alarm_instances", [name], unique=False)


def downgrade() -> None:
    for name in ("opened_at","state","severity","event_type","camera_id","site_id","tenant_id","event_id","rule_id"):
        op.drop_index(f"ix_alarm_instances_{name}", table_name="alarm_instances")
    op.drop_table("alarm_instances")
    op.drop_index("ix_alarm_rules_enabled", table_name="alarm_rules")
    op.drop_index("ix_alarm_rules_site_id", table_name="alarm_rules")
    op.drop_index("ix_alarm_rules_tenant_id", table_name="alarm_rules")
    op.drop_table("alarm_rules")
