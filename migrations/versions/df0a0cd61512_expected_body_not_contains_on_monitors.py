"""expected_body_not_contains on monitors

Revision ID: df0a0cd61512
Revises: c47d90a2f6b1
Create Date: 2026-10-26 01:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'df0a0cd61512'
down_revision: Union[str, None] = 'c47d90a2f6b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable with no server_default: NULL is exactly what the ORM means by
    # "no body assertion", which is what every existing monitor has.
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.add_column(
            sa.Column('expected_body_not_contains', sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.drop_column('expected_body_not_contains')
