"""media gateway connection bindings (ADR-0015)

Revision ID: 0002_media_connections
Revises: 0001_initial
Create Date: 2026-10-22 00:00:00.000000+00:00

Additive only: one new table, no change to existing tables or data.
Downgrade drops the table; open bindings are lost, which only removes the
ability to target already-connected viewers until they reconnect.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.base import UtcDateTime

revision = '0002_media_connections'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'media_connections',
        sa.Column('live_session_id', sa.String(length=32), nullable=False),
        sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('camera_id', sa.String(length=32), nullable=False),
        sa.Column('path', sa.String(length=256), nullable=False),
        sa.Column('protocol', sa.String(length=16), nullable=False),
        sa.Column('gateway_conn_id', sa.String(length=128), nullable=False),
        sa.Column('created_at', UtcDateTime(), nullable=False),
        sa.Column('last_admitted_at', UtcDateTime(), nullable=False),
        sa.Column('last_policy_check_at', UtcDateTime(), nullable=True),
        sa.Column('revoke_reason', sa.String(length=64), nullable=True),
        sa.Column('revoked_at', UtcDateTime(), nullable=True),
        sa.Column('kick_attempts', sa.Integer(), nullable=False),
        sa.Column('kicked_at', UtcDateTime(), nullable=True),
        sa.Column('closed_at', UtcDateTime(), nullable=True),
        sa.Column('close_reason', sa.String(length=64), nullable=True),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_media_connections')),
        sa.UniqueConstraint('protocol', 'gateway_conn_id',
                            name='media_conn_unique_gateway_id'),
    )
    with op.batch_alter_table('media_connections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_media_connections_camera_id'),
                              ['camera_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_media_connections_closed_at'),
                              ['closed_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_media_connections_live_session_id'),
                              ['live_session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_media_connections_user_id'),
                              ['user_id'], unique=False)
        batch_op.create_index('ix_media_conn_open_session',
                              ['live_session_id', 'closed_at'], unique=False)
        batch_op.create_index('ix_media_conn_policy_scan',
                              ['closed_at', 'revoke_reason', 'last_policy_check_at'],
                              unique=False)


def downgrade() -> None:
    with op.batch_alter_table('media_connections', schema=None) as batch_op:
        batch_op.drop_index('ix_media_conn_policy_scan')
        batch_op.drop_index('ix_media_conn_open_session')
        batch_op.drop_index(batch_op.f('ix_media_connections_user_id'))
        batch_op.drop_index(batch_op.f('ix_media_connections_live_session_id'))
        batch_op.drop_index(batch_op.f('ix_media_connections_closed_at'))
        batch_op.drop_index(batch_op.f('ix_media_connections_camera_id'))
    op.drop_table('media_connections')
