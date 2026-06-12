"""Tests for create_pool_with_retry — the tool-server's startup-race guard."""

from __future__ import annotations

import asyncpg
import pytest


@pytest.mark.asyncio
async def test_retries_until_endpoint_accepts(monkeypatch) -> None:
    """ConnectionRefusedError on early attempts is retried, then succeeds."""
    from api import db

    sentinel_pool = object()
    attempts = {"n": 0}

    async def fake_create_pool(
        database_url,
        *,
        apply_migrations=True,
        min_size=2,
        max_size=10,
        proxy_safe_reset=False,
    ):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionRefusedError(111, "Connection refused")
        return sentinel_pool

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(db, "create_pool", fake_create_pool)
    monkeypatch.setattr(db.asyncio, "sleep", fake_sleep)

    pool = await db.create_pool_with_retry(
        "postgres://localhost/db", apply_migrations=False, base_delay=0.5
    )

    assert pool is sentinel_pool
    assert attempts["n"] == 3
    # Backoff applied once per failed attempt (two failures before success).
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_pool_size_is_forwarded(monkeypatch) -> None:
    """min_size/max_size pass through to create_pool (tool-server uses 1/1)."""
    from api import db

    captured: dict[str, int] = {}

    async def fake_create_pool(
        database_url,
        *,
        apply_migrations=True,
        min_size,
        max_size,
        proxy_safe_reset=False,
    ):
        captured["min_size"] = min_size
        captured["max_size"] = max_size
        return object()

    monkeypatch.setattr(db, "create_pool", fake_create_pool)

    await db.create_pool_with_retry(
        "postgres://localhost/db", apply_migrations=False, min_size=1, max_size=1
    )

    assert captured == {"min_size": 1, "max_size": 1}


@pytest.mark.asyncio
async def test_proxy_safe_reset_is_forwarded(monkeypatch) -> None:
    """tool-server pools can opt into an iron-proxy-safe reset hook."""
    from api import db

    captured: dict[str, object] = {}

    async def fake_asyncpg_create_pool(database_url, **kwargs):
        captured["database_url"] = database_url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_asyncpg_create_pool)
    monkeypatch.setattr(db, "run_migrations", lambda _database_url: None)

    await db.create_pool("postgres://localhost/db", proxy_safe_reset=True)

    assert captured["database_url"] == "postgres://localhost/db"
    assert captured["reset"] is db.proxy_safe_connection_reset


@pytest.mark.asyncio
async def test_proxy_safe_connection_reset_splits_default_reset_query() -> None:
    from api import db

    class FakeConnection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def get_reset_query(self) -> str:
            return (
                "SELECT pg_advisory_unlock_all();\n"
                "CLOSE ALL;\n"
                "UNLISTEN *;\n"
                "RESET ALL;"
            )

        async def execute(self, statement: str) -> None:
            self.statements.append(statement)

    conn = FakeConnection()

    await db.proxy_safe_connection_reset(conn)

    assert conn.statements == [
        "SELECT pg_advisory_unlock_all()",
        "CLOSE ALL",
        "UNLISTEN *",
        "RESET ALL",
    ]


@pytest.mark.asyncio
async def test_backoff_is_capped(monkeypatch) -> None:
    from api import db

    async def always_refused(
        database_url,
        *,
        apply_migrations=True,
        min_size=2,
        max_size=10,
        proxy_safe_reset=False,
    ):
        raise ConnectionRefusedError(111, "Connection refused")

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(db, "create_pool", always_refused)
    monkeypatch.setattr(db.asyncio, "sleep", fake_sleep)

    with pytest.raises(ConnectionRefusedError):
        await db.create_pool_with_retry(
            "postgres://localhost/db",
            apply_migrations=False,
            max_attempts=10,
            base_delay=0.5,
            max_delay=5.0,
        )

    # No sleep after the final (exhausting) attempt.
    assert len(sleeps) == 9
    assert max(sleeps) == 5.0
    assert sleeps[-1] == 5.0


@pytest.mark.asyncio
async def test_postgres_starting_up_is_retried(monkeypatch) -> None:
    """57P03 (CannotConnectNowError) is a transient startup condition."""
    from api import db

    sentinel_pool = object()
    attempts = {"n": 0}

    async def fake_create_pool(
        database_url,
        *,
        apply_migrations=True,
        min_size=2,
        max_size=10,
        proxy_safe_reset=False,
    ):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise asyncpg.CannotConnectNowError("the database system is starting up")
        return sentinel_pool

    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(db, "create_pool", fake_create_pool)
    monkeypatch.setattr(db.asyncio, "sleep", fake_sleep)

    pool = await db.create_pool_with_retry(
        "postgres://localhost/db", apply_migrations=False
    )

    assert pool is sentinel_pool
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_any_error_is_retried_then_reraised(monkeypatch) -> None:
    """Every exception is retried; the last is re-raised once exhausted."""
    from api import db

    attempts = {"n": 0}

    async def fake_create_pool(
        database_url,
        *,
        apply_migrations=True,
        min_size=2,
        max_size=10,
        proxy_safe_reset=False,
    ):
        attempts["n"] += 1
        raise ValueError("boom")

    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(db, "create_pool", fake_create_pool)
    monkeypatch.setattr(db.asyncio, "sleep", fake_sleep)

    with pytest.raises(ValueError):
        await db.create_pool_with_retry(
            "postgres://localhost/db", apply_migrations=False, max_attempts=4
        )

    assert attempts["n"] == 4
