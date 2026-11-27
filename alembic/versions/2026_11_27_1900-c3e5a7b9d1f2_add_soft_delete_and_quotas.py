"""soft delete (documents.deleted_at) + per-user quota overrides

Revision ID: c3e5a7b9d1f2
Revises: b2d4f6a8c0e1
Create Date: 2026-11-27 19:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3e5a7b9d1f2'
down_revision: Union[str, None] = 'b2d4f6a8c0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    # Partial index: only trashed rows are indexed (a tiny index), which is
    # exactly what the purge job looks for.
    op.create_index('ix_documents_deleted_at', 'documents', ['deleted_at'],
                    postgresql_where=sa.text('deleted_at IS NOT NULL'))
    # NULL = use the default from settings.
    op.add_column('users', sa.Column('quota_max_documents', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('quota_max_storage_mb', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('quota_max_questions_per_day', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'quota_max_questions_per_day')
    op.drop_column('users', 'quota_max_storage_mb')
    op.drop_column('users', 'quota_max_documents')
    op.drop_index('ix_documents_deleted_at', table_name='documents')
    op.drop_column('documents', 'deleted_at')
