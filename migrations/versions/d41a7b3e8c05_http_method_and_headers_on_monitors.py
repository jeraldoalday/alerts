"""http method and headers on monitors

Revision ID: d41a7b3e8c05
Revises: c92e4a7b1f38
Create Date: 2026-08-21 09:14:02.118904
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd41a7b3e8c05'
down_revision: Union[str, None] = 'c92e4a7b1f38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Both columns are NOT NULL, so existing rows need a server_default to be
    # backfilled with -- 'GET' and an empty header map, which is exactly what
    # every monitor configured before this migration was already doing.
    #
    # The defaults are dropped again afterwards so the ORM's Python-side
    # defaults are the only ones in effect going forward, matching how
    # 1ec36c9f4b5c added `channels`. Separate batch blocks per step: SQLite's
    # batch mode rebuilds the table once per block, and the server_default
    # has to be applied to the copied rows before a second rebuild can
    # remove it.
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.add_column(
            sa.Column('method', sa.String(length=10), nullable=False, server_default='GET')
        )
        batch_op.add_column(
            sa.Column('headers', sa.JSON(), nullable=False, server_default='{}')
        )
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.alter_column('headers', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.drop_column('headers')
        batch_op.drop_column('method')
