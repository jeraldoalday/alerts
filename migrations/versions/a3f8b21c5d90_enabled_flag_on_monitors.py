"""enabled flag on monitors

Revision ID: a3f8b21c5d90
Revises: 7c4a1de90fb2
Create Date: 2026-08-20 08:41:07.552104
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f8b21c5d90'
down_revision: Union[str, None] = '7c4a1de90fb2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Every pre-existing monitor was being checked, so they all upgrade to
    # enabled. The next `alerts run` re-syncs the config and turns off
    # whichever ones are no longer declared there.
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.add_column(
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1')
        )


def downgrade() -> None:
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.drop_column('enabled')
