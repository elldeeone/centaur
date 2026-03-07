"""Add chat_messages table for Next.js UIMessage persistence.

Revision ID: 003
Revises: 002
"""

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE chat_messages (
            id          TEXT PRIMARY KEY,
            thread_key  TEXT NOT NULL,
            role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
            parts       JSONB NOT NULL DEFAULT '[]',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            metadata    JSONB DEFAULT '{}'
        )
    """)
    op.execute(
        "CREATE INDEX idx_chat_messages_thread_key "
        "ON chat_messages (thread_key, created_at)"
    )
    op.execute(
        "CREATE INDEX idx_chat_messages_created_at "
        "ON chat_messages (created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_chat_messages_thread_summary "
        "ON chat_messages (thread_key, created_at DESC) "
        "INCLUDE (role, metadata)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chat_messages CASCADE")
