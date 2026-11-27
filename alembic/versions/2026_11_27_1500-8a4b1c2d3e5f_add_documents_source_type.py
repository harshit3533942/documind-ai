"""add documents.source_type (pdf / docx / txt / md)

Revision ID: 8a4b1c2d3e5f
Revises: 7d2e9c41b8a3
Create Date: 2026-11-27 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8a4b1c2d3e5f'
down_revision: Union[str, None] = '7d2e9c41b8a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A constant server default is metadata-only since PostgreSQL 11: existing
    # rows read 'pdf' without the table being rewritten.
    op.add_column('documents', sa.Column('source_type', sa.String(length=10), server_default='pdf', nullable=False))
    op.create_check_constraint(
        op.f('ck_documents_source_type_valid'), 'documents', "source_type IN ('pdf', 'docx', 'txt', 'md')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f('ck_documents_source_type_valid'), 'documents', type_='check')
    op.drop_column('documents', 'source_type')
