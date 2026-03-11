"""Sandbox-specific DB schema — sandbox_sessions + chat_messages tables."""

from __future__ import annotations

import asyncpg
import structlog

log = structlog.get_logger()


async def ensure_sandbox_schema(pool: asyncpg.Pool) -> None:
    """Create sandbox_sessions and chat_messages tables if they don't exist."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sandbox_sessions (
                thread_key   TEXT PRIMARY KEY,
                sandbox_id   TEXT NOT NULL,
                channel_id   TEXT NOT NULL DEFAULT '',
                thread_ts    TEXT NOT NULL DEFAULT '',
                harness      TEXT NOT NULL DEFAULT 'amp',
                engine       TEXT NOT NULL DEFAULT 'amp',
                state        TEXT NOT NULL DEFAULT 'creating'
                             CHECK (state IN ('creating','running','stopped','gone')),
                config_sent  BOOLEAN NOT NULL DEFAULT FALSE,
                thread_name  TEXT,
                started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id          TEXT PRIMARY KEY,
                thread_key  TEXT NOT NULL,
                role        TEXT NOT NULL,
                parts       JSONB NOT NULL DEFAULT '[]',
                metadata    JSONB NOT NULL DEFAULT '{}',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_thread "
            "ON chat_messages (thread_key, created_at)"
        )
    log.info("sandbox_schema_ensured")
