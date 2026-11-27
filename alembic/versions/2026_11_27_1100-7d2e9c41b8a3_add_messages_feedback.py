"""add messages.feedback (thumbs up / down on answers)

Revision ID: 7d2e9c41b8a3
Revises: 2574d69e4968
Create Date: 2026-11-27 11:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7d2e9c41b8a3'
down_revision: Union[str, None] = '2574d69e4968'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable column without a default: a metadata-only change in PostgreSQL,
    # no table rewrite even with millions of messages.
    op.add_column('messages', sa.Column('feedback', sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        op.f('ck_messages_feedback_valid'), 'messages', 'feedback IS NULL OR feedback IN (-1, 1)'
    )


def downgrade() -> None:
    op.drop_constraint(op.f('ck_messages_feedback_valid'), 'messages', type_='check')
    op.drop_column('messages', 'feedback')
