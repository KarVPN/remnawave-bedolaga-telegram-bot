"""Durable Telegram receipts for bulk subscription changes.

Revision ID: 0133
Revises: 0132
"""

from alembic import op
import sqlalchemy as sa

revision = '0133'
down_revision = '0132'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_bulk_notifications',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'operation_id', sa.String(36), sa.ForeignKey('user_bulk_operations.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('text', sa.Text()),
        sa.Column('message', sa.Text()),
        sa.Column('telegram_message_id', sa.BigInteger()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('operation_id', 'user_id', name='uq_user_bulk_notification_recipient'),
    )
    op.create_index('ix_user_bulk_notification_pending', 'user_bulk_notifications', ['operation_id', 'state', 'id'])


def downgrade():
    op.drop_table('user_bulk_notifications')
