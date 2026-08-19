"""consecutive_fast_checks on monitors

Revision ID: 7c4a1de90fb2
Revises: 2d77aa3d7814
Create Date: 2026-08-20 08:12:44.118902
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7c4a1de90fb2'
down_revision: Union[str, None] = '2d77aa3d7814'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL with a server_default of 0: every existing monitor starts with
    # an empty fast-check streak, which is the safe direction -- a degraded
    # monitor has to earn its way back to UP after the upgrade rather than
    # being handed a streak it never ran.
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.add_column(
            sa.Column(
                'consecutive_fast_checks',
                sa.Integer(),
                nullable=False,
                server_default='0',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.drop_column('consecutive_fast_checks')
