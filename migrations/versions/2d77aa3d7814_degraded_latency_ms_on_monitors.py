"""degraded_latency_ms on monitors

Revision ID: 2d77aa3d7814
Revises: 1ec36c9f4b5c
Create Date: 2026-08-19 21:21:32.960359
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2d77aa3d7814'
down_revision: Union[str, None] = '1ec36c9f4b5c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable with no server_default: an existing monitor having no latency
    # budget is exactly the NULL the ORM means, so there's nothing to backfill
    # and no second batch block needed to drop a default afterwards.
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.add_column(sa.Column('degraded_latency_ms', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('monitors') as batch_op:
        batch_op.drop_column('degraded_latency_ms')
