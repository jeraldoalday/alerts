"""depends_on on monitors

Revision ID: c47d90a2f6b1
Revises: f3b0d5c81e27
Create Date: 2026-09-01 08:41:52.117364
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c47d90a2f6b1'
down_revision: Union[str, None] = 'f3b0d5c81e27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # An existing monitor depends on nothing, which is also what the column
    # has to say about it before the next config sync writes the real list.
    # The server_default backfills the existing rows and then stays: unlike
    # `tags`, the model declares one too, so that a row inserted by anything
    # that doesn't go through the ORM still gets a list rather than a NULL
    # the graph would have to defend against.
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.add_column(
            sa.Column('depends_on', sa.JSON(), nullable=False, server_default='[]')
        )


def downgrade() -> None:
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.drop_column('depends_on')
