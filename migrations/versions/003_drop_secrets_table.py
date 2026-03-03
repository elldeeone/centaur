"""Drop unused secrets table.

The secrets table was a vestigial prototype that stored plaintext secrets
in Postgres. All secret resolution goes through the 1Password-backed
secret_manager sidecar. Removing this table eliminates a dangerous attack
surface (raw SQL endpoint could dump plaintext credentials).

Revision ID: 003
Revises: 002
"""

from alembic import op

revision = "003"
down_revision = "002"


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS secrets")


def downgrade() -> None:
    op.execute("""
        CREATE TABLE secrets (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            source      TEXT,
            description TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
