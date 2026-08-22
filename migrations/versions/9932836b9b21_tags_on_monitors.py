"""tags on monitors

Revision ID: 9932836b9b21
Revises: d41a7b3e8c05
Create Date: 2026-08-23 07:20:11.412093
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9932836b9b21'
down_revision: Union[str, None] = 'd41a7b3e8c05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing rows with an empty tag list, then is
    # dropped so the ORM's Python-side `default=list` is the only default in
    # effect -- the same two-block shape 1ec36c9f4b5c used for `channels`,
    # since SQLite's batch mode rebuilds the table once per block and the
    # default has to be applied to the copied rows before a second rebuild
    # can drop it.
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.add_column(
            sa.Column('tags', sa.JSON(), nullable=False, server_default='[]')
        )
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.alter_column('tags', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.drop_column('tags')
