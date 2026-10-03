"""AI models and camera policies

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-25
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "ai_models",
        sa.Column("id",sa.String(length=36),nullable=False),
        sa.Column("tenant_id",sa.String(length=128),nullable=False),
        sa.Column("name",sa.String(length=256),nullable=False),
        sa.Column("version",sa.String(length=128),nullable=False),
        sa.Column("provider_type",sa.String(length=32),nullable=False),
        sa.Column("artifact_ref",sa.String(length=512),nullable=False),
        sa.Column("sha256",sa.String(length=64),nullable=True),
        sa.Column("labels_json",sa.JSON(),nullable=False),
        sa.Column("input_width",sa.Integer(),nullable=True),
        sa.Column("input_height",sa.Integer(),nullable=True),
        sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.true()),
        sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.text("CURRENT_TIMESTAMP"),nullable=False),
        sa.Column("updated_at",sa.DateTime(timezone=True),server_default=sa.text("CURRENT_TIMESTAMP"),nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id","name","version",name="uq_ai_model_tenant_name_version"),
    )
    op.create_index("ix_ai_models_tenant_id","ai_models",["tenant_id"],unique=False)
    op.create_index("ix_ai_models_enabled","ai_models",["enabled"],unique=False)
    op.create_table(
        "camera_ai_policies",
        sa.Column("id",sa.String(length=36),nullable=False),
        sa.Column("camera_id",sa.String(length=36),nullable=False),
        sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column("source_mode",sa.String(length=32),nullable=False,server_default="inference"),
        sa.Column("model_id",sa.String(length=36),nullable=True),
        sa.Column("stream_role",sa.String(length=16),nullable=False,server_default="sub"),
        sa.Column("sample_fps",sa.Float(),nullable=False,server_default="1"),
        sa.Column("min_confidence",sa.Float(),nullable=False,server_default="0.5"),
        sa.Column("analytics_json",sa.JSON(),nullable=False),
        sa.Column("zones_json",sa.JSON(),nullable=False),
        sa.Column("provider_config_json",sa.JSON(),nullable=False),
        sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.text("CURRENT_TIMESTAMP"),nullable=False),
        sa.Column("updated_at",sa.DateTime(timezone=True),server_default=sa.text("CURRENT_TIMESTAMP"),nullable=False),
        sa.ForeignKeyConstraint(["camera_id"],["cameras.id"],ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"],["ai_models.id"],ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("camera_id",name="uq_camera_ai_policy_camera_id"),
    )
    op.create_index("ix_camera_ai_policies_enabled","camera_ai_policies",["enabled"],unique=False)
    op.create_index("ix_camera_ai_policies_model_id","camera_ai_policies",["model_id"],unique=False)

def downgrade() -> None:
    op.drop_index("ix_camera_ai_policies_model_id",table_name="camera_ai_policies")
    op.drop_index("ix_camera_ai_policies_enabled",table_name="camera_ai_policies")
    op.drop_table("camera_ai_policies")
    op.drop_index("ix_ai_models_enabled",table_name="ai_models")
    op.drop_index("ix_ai_models_tenant_id",table_name="ai_models")
    op.drop_table("ai_models")
