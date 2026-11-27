"""email verification + password reset: users.email_verified_at, user_tokens

Revision ID: b2d4f6a8c0e1
Revises: a1c3e5f7b9d2
Create Date: 2026-11-27 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2d4f6a8c0e1'
down_revision: Union[str, None] = 'a1c3e5f7b9d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing accounts start unverified (NULL); they can request a link from the app.
    op.add_column('users', sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'user_tokens',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('purpose', sa.Enum('verify_email', 'reset_password', name='token_purpose',
                                     native_enum=False, create_constraint=True, length=20), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_tokens_user_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_user_tokens')),
        sa.UniqueConstraint('token_hash', name=op.f('uq_user_tokens_token_hash')),
    )
    op.create_index('ix_user_tokens_user_id_purpose', 'user_tokens', ['user_id', 'purpose'])
    op.create_index('ix_user_tokens_expires_at', 'user_tokens', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_user_tokens_expires_at', table_name='user_tokens')
    op.drop_index('ix_user_tokens_user_id_purpose', table_name='user_tokens')
    op.drop_table('user_tokens')
    op.drop_column('users', 'email_verified_at')
