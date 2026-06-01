from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clear_slack_context_tables(db_pool, monkeypatch):
    monkeypatch.setenv("SLACK_ETL_ENABLED", "true")
    monkeypatch.setenv("SLACK_EVENT_INGEST_PROJECT_INLINE", "true")
    monkeypatch.setenv("COMPANY_CONTEXT_DOCUMENTS_ENABLED", "true")
    await db_pool.execute(
        "TRUNCATE TABLE company_context_documents, slack_sync_backfill_jobs, "
        "slack_sync_checkpoints, slack_sync_messages, slack_sync_runs, "
        "slack_sync_users, slack_sync_channels CASCADE",
    )
    yield


def _envelope(event: dict[str, Any], event_id: str = "Ev-ingest") -> dict[str, Any]:
    return {
        "type": "event_callback",
        "team_id": "T123",
        "event_id": event_id,
        "event": event,
    }


@pytest.mark.asyncio
async def test_slack_event_ingest_upserts_message_and_projects_context(client, db_pool):
    response = await client.post(
        "/api/slack/events/ingest",
        json={
            "envelope": _envelope(
                {
                    "type": "message",
                    "user": "U123",
                    "channel": "C_PUBLIC",
                    "ts": "1780000000.000001",
                    "text": "Fresh cross-channel context for Centaur",
                },
            ),
            "project_context": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ingested"
    assert body["projected"] is True

    row = await db_pool.fetchrow(
        "SELECT channel_id, message_ts, user_id, text, source_run_id "
        "FROM slack_sync_messages WHERE channel_id = 'C_PUBLIC'",
    )
    assert row is not None
    assert row["message_ts"] == "1780000000.000001"
    assert row["user_id"] == "U123"
    assert row["text"] == "Fresh cross-channel context for Centaur"
    assert row["source_run_id"] is None

    document = await db_pool.fetchval(
        "SELECT body FROM company_context_documents "
        "WHERE source = 'slack' AND source_type = 'channel_day' LIMIT 1",
    )
    assert "Fresh cross-channel context for Centaur" in document


@pytest.mark.asyncio
async def test_slack_event_ingest_updates_changed_and_deleted_messages(
    client,
    db_pool,
    monkeypatch,
):
    monkeypatch.setenv("SLACK_EVENT_INGEST_PROJECT_INLINE", "false")
    initial = _envelope(
        {
            "type": "message",
            "user": "U123",
            "channel": "C_PUBLIC",
            "ts": "1780000000.000002",
            "text": "original text",
        },
        event_id="Ev-original",
    )
    changed = _envelope(
        {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C_PUBLIC",
            "message": {
                "type": "message",
                "user": "U123",
                "channel": "C_PUBLIC",
                "ts": "1780000000.000002",
                "text": "edited text",
            },
            "previous_message": {
                "type": "message",
                "user": "U123",
                "channel": "C_PUBLIC",
                "ts": "1780000000.000002",
                "text": "original text",
            },
        },
        event_id="Ev-changed",
    )
    deleted = _envelope(
        {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "C_PUBLIC",
            "deleted_ts": "1780000000.000002",
            "previous_message": {
                "type": "message",
                "user": "U123",
                "channel": "C_PUBLIC",
                "ts": "1780000000.000002",
                "text": "edited text",
            },
        },
        event_id="Ev-deleted",
    )

    for envelope in (initial, changed, deleted):
        response = await client.post(
            "/api/slack/events/ingest",
            json={"envelope": envelope, "project_context": True},
        )
        assert response.status_code == 200

    row = await db_pool.fetchrow(
        "SELECT text, message_subtype, raw_payload "
        "FROM slack_sync_messages "
        "WHERE channel_id = 'C_PUBLIC' AND message_ts = '1780000000.000002'",
    )
    assert row is not None
    assert row["text"] == ""
    assert row["message_subtype"] == "message_deleted"
    assert row["raw_payload"]["event_id"] == "Ev-deleted"
