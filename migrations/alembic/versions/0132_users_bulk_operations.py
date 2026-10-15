"""Durable Users bulk operations, no rollback snapshots.

Revision ID: 0132
Revises: 0131
"""

from alembic import op
import sqlalchemy as sa

revision = '0132'
down_revision = '0131'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_bulk_operations',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('author_id', sa.Integer(), nullable=False),
        sa.Column('request_key', sa.String(36), nullable=False),
        sa.Column('action', sa.String(40), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('author_id', 'request_key'),
    )
    op.create_index('ix_user_bulk_operations_author_id', 'user_bulk_operations', ['author_id'])
    op.create_table(
        'user_bulk_targets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('operation_id', sa.String(36), sa.ForeignKey('user_bulk_operations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('subscription_id', sa.Integer()),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('message', sa.Text()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_user_bulk_target_pending', 'user_bulk_targets', ['operation_id', 'state', 'id'])
    op.create_index('ix_user_bulk_target_user', 'user_bulk_targets', ['operation_id', 'user_id'])


def downgrade():
    op.drop_table('user_bulk_targets')
    op.drop_table('user_bulk_operations')
