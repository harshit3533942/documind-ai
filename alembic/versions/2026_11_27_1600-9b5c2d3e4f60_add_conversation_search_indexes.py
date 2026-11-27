"""conversation search: pg_trgm GIN indexes on conversations.title, messages.content

Revision ID: 9b5c2d3e4f60
Revises: 8a4b1c2d3e5f
Create Date: 2026-11-27 16:00:00

"""
from typing import Sequence, Union

from alembic import op


revision: str = '9b5c2d3e4f60'
down_revision: Union[str, None] = '8a4b1c2d3e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pg_trgm ships with PostgreSQL (contrib). Trigram GIN indexes make
    # ILIKE '%term%' searches index-assisted instead of full table scans.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index('ix_conversations_title_trgm', 'conversations', ['title'],
                    postgresql_using='gin', postgresql_ops={'title': 'gin_trgm_ops'})
    op.create_index('ix_messages_content_trgm', 'messages', ['content'],
                    postgresql_using='gin', postgresql_ops={'content': 'gin_trgm_ops'})


def downgrade() -> None:
    op.drop_index('ix_messages_content_trgm', table_name='messages')
    op.drop_index('ix_conversations_title_trgm', table_name='conversations')
    # The extension is left installed: other objects may depend on it.
