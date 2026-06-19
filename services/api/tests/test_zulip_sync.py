from __future__ import annotations

import datetime as dt
from typing import Any

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


def test_zulip_etl_client_uses_thread_key_realm_for_hosted_zulip(
    monkeypatch: pytest.MonkeyPatch,
):
    from workflows.zulip_sync_shared import ZulipEtlClient

    monkeypatch.setenv("ZULIP_ETL_SITE", "https://staghunt.zulipchat.com")
    monkeypatch.setenv("ZULIP_ETL_EMAIL", "centaur-bot@staghunt.zulipchat.com")
    monkeypatch.setenv("ZULIP_ETL_API_KEY", "test-api-key")
    monkeypatch.delenv("ZULIP_ETL_REALM", raising=False)

    client = ZulipEtlClient()

    assert client.realm == "staghunt"


def test_zulip_etl_client_allows_explicit_thread_key_realm(
    monkeypatch: pytest.MonkeyPatch,
):
    from workflows.zulip_sync_shared import ZulipEtlClient

    monkeypatch.setenv("ZULIP_ETL_SITE", "https://chat.example.com")
    monkeypatch.setenv("ZULIP_ETL_EMAIL", "centaur-bot@example.com")
    monkeypatch.setenv("ZULIP_ETL_API_KEY", "test-api-key")
    monkeypatch.setenv("ZULIP_ETL_REALM", "Stag Hunt")

    client = ZulipEtlClient()

    assert client.realm == "stag-hunt"


def test_zulip_etl_client_falls_back_when_optional_etl_secret_is_absent(
    monkeypatch: pytest.MonkeyPatch,
):
    from workflows import zulip_sync_shared

    def missing_secret(name: str, *, default: str = "") -> str:
        return name

    monkeypatch.setattr(zulip_sync_shared, "secret", missing_secret)
    monkeypatch.delenv("ZULIP_ETL_SITE", raising=False)
    monkeypatch.delenv("ZULIP_ETL_EMAIL", raising=False)
    monkeypatch.delenv("ZULIP_ETL_API_KEY", raising=False)
    monkeypatch.delenv("ZULIP_ETL_REALM", raising=False)
    monkeypatch.setenv("ZULIP_SITE", "https://staghunt.zulipchat.com")
    monkeypatch.setenv("ZULIP_BOT_EMAIL", "centaur-bot@staghunt.zulipchat.com")
    monkeypatch.setenv("ZULIP_API_KEY", "fallback-api-key")

    client = zulip_sync_shared.ZulipEtlClient()

    assert client.site == "https://staghunt.zulipchat.com"
    assert client.email == "centaur-bot@staghunt.zulipchat.com"
    assert client.api_key == "fallback-api-key"
    assert client.realm == "staghunt"


def test_list_etl_streams_uses_non_admin_visibility_params():
    from workflows.zulip_sync_shared import ZulipEtlClient

    client = ZulipEtlClient.__new__(ZulipEtlClient)
    client._ratelimit_deadlines = {}
    calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any],
        method_key: str,
    ) -> dict[str, Any]:
        calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "method_key": method_key,
            }
        )
        return {"streams": [{"stream_id": 42, "name": "research"}]}

    client._request = fake_request

    assert client._list_etl_streams() == [{"stream_id": 42, "name": "research"}]
    assert calls == [
        {
            "method": "GET",
            "path": "/api/v1/streams",
            "params": {
                "include_public": "true",
                "include_web_public": "true",
                "include_subscribed": "true",
            },
            "method_key": "streams",
        }
    ]
    assert "include_all_active" not in calls[0]["params"]


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
