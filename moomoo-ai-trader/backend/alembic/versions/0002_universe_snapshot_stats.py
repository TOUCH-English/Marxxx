"""universe snapshot stats

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23 22:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('universe_snapshot') as batch:
        batch.add_column(sa.Column('stats', sa.JSON(), nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('universe_snapshot') as batch:
        batch.drop_column('stats')
