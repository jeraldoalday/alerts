"""slo target on monitors

Revision ID: f3b0d5c81e27
Revises: 9932836b9b21
Create Date: 2026-08-24 08:14:52.106884
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3b0d5c81e27'
down_revision: Union[str, None] = '9932836b9b21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `slo_target` is nullable and needs no backfill: NULL is exactly what an
    # existing monitor should mean, which is that nobody has promised
    # anything about it yet.
    #
    # `slo_window_days` is NOT NULL, so it takes the same two-block shape the
    # earlier column additions used -- a server_default to fill the existing
    # rows, then a second batch block to drop it, since SQLite's batch mode
    # rebuilds the table once per block and the default has to reach the
    # copied rows before a later rebuild can remove it. The value left behind
    # is only ever read alongside a target, and every existing row has none.
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.add_column(sa.Column('slo_target', sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'slo_window_days', sa.Integer(), nullable=False, server_default='30'
            )
        )
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.alter_column('slo_window_days', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.drop_column('slo_window_days')
        batch_op.drop_column('slo_target')
