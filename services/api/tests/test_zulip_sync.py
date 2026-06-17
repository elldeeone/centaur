from __future__ import annotations

import datetime as dt

import pytest


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple]] = []

    def transaction(self):
        return _AsyncContext(self)

    async def execute(self, query: str, *args):
        self.executes.append((query, args))


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConnection()

    def acquire(self):
        return _AsyncContext(self.conn)


@pytest.mark.asyncio
async def test_upsert_messages_counts_only_rows_written():
    from workflows.zulip_sync_shared import upsert_messages

    pool = _FakePool()
    rows = [
        {
            "realm": "staghunt.zulipchat.com",
            "message_id": 123,
            "stream_id": 42,
            "topic_name": "roadmap",
            "occurred_at": dt.datetime(2026, 6, 17, tzinfo=dt.UTC),
            "sender_id": 7,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "recipient_id": 42,
            "message_type": "stream",
            "content": "valid",
            "rendered_content": "<p>valid</p>",
            "subject": "roadmap",
            "permalink": "https://zulip.example/#narrow/near/123",
            "raw_payload": {"id": 123},
            "source_run_id": "zulip_sync_test",
        },
        {
            "realm": "staghunt.zulipchat.com",
            "message_id": 0,
            "stream_id": 42,
        },
        {
            "realm": "staghunt.zulipchat.com",
            "message_id": 124,
            "stream_id": 0,
        },
    ]

    assert await upsert_messages(pool, rows) == 1
    assert len(pool.conn.executes) == 1
