"""expected_body_contains on monitors

Revision ID: c92e4a7b1f38
Revises: a3f8b21c5d90
Create Date: 2026-08-20 09:27:19.874331
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c92e4a7b1f38'
down_revision: Union[str, None] = 'a3f8b21c5d90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable with no server_default: NULL is exactly what the ORM means by
    # "no body assertion", which is what every existing monitor has.
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.add_column(
            sa.Column('expected_body_contains', sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.drop_column('expected_body_contains')
