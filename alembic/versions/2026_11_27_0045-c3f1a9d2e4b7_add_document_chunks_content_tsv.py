"""add document_chunks.content_tsv (full-text search for hybrid retrieval)

Revision ID: c3f1a9d2e4b7
Revises: 546fea95f2d8
Create Date: 2026-11-27 00:45:00.000000

A STORED generated column: PostgreSQL fills it for every existing row during
this migration and keeps it in sync on every insert/update afterwards.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c3f1a9d2e4b7'
down_revision: Union[str, None] = '546fea95f2d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'document_chunks',
        sa.Column(
            'content_tsv',
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english'::regconfig, content)", persisted=True),
            nullable=True,
        ),
    )
    op.create_index('ix_document_chunks_content_tsv', 'document_chunks', ['content_tsv'], unique=False, postgresql_using='gin')


def downgrade() -> None:
    op.drop_index('ix_document_chunks_content_tsv', table_name='document_chunks', postgresql_using='gin')
    op.drop_column('document_chunks', 'content_tsv')
