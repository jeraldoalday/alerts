"""channels on monitors

Revision ID: 1ec36c9f4b5c
Revises: 551cff07e06e
Create Date: 2026-08-15 15:32:17.189089
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1ec36c9f4b5c'
down_revision: Union[str, None] = '551cff07e06e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing rows with an empty channel list;
    # dropped after so the ORM's Python-side `default=list` is the only one
    # in effect going forward. Two batch blocks, as in 551cff07e06e, since
    # SQLite's batch mode rebuilds the table once per block and the
    # server_default must already be applied to the copied rows before a
    # second rebuild can drop it.
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.add_column(
            sa.Column('channels', sa.JSON(), nullable=False, server_default='[]')
        )
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.alter_column('channels', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.drop_column('channels')
