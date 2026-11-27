"""add documents.summary, suggested_questions, summarized_at

Revision ID: a1c3e5f7b9d2
Revises: 9b5c2d3e4f60
Create Date: 2026-11-27 17:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a1c3e5f7b9d2'
down_revision: Union[str, None] = '9b5c2d3e4f60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, no default: metadata-only. Existing documents simply have no
    # summary yet (the web app offers to generate one).
    op.add_column('documents', sa.Column('summary', sa.Text(), nullable=True))
    op.add_column('documents', sa.Column('suggested_questions', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('documents', sa.Column('summarized_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('documents', 'summarized_at')
    op.drop_column('documents', 'suggested_questions')
    op.drop_column('documents', 'summary')
