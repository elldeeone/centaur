from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

import client as company_context_client
from client import CompanyContextClient


class _FakeConnection:
    def __init__(self, *, rows=None, row=None) -> None:
        self.rows = rows or []
        self.row = row
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.closed = False

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.rows

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.row

    async def close(self):
        self.closed = True


class _FakeSlackClient:
    def __init__(self, messages=None) -> None:
        self.messages = messages or []
        self.calls = []

    def search_messages(self, query, max_results=20):
        self.calls.append((query, max_results))
        return self.messages


@pytest.mark.parametrize("query", ["", "   "])
def test_search_rejects_empty_query(query):
    result = CompanyContextClient("postgresql://example").search(query)

    assert result == {"status": "error", "error": "query cannot be empty"}


def test_search_queries_bm25_and_returns_compact_results(monkeypatch):
    occurred_at = dt.datetime(2026, 5, 8, 12, 0, tzinfo=dt.UTC)
    source_updated_at = dt.datetime(2026, 5, 8, 12, 5, tzinfo=dt.UTC)
    fake = _FakeConnection(
        rows=[
            {
                "document_id": "slack:thread:C123:1770000000.000000",
                "source": "slack",
                "source_type": "slack_thread",
                "title": "BM25 indexing plan",
                "url": "https://slack.example/thread",
                "occurred_at": occurred_at,
                "source_updated_at": source_updated_at,
                "metadata": {"channel_name": "eng-ai", "thread_ts": "1770000000.000000"},
                "score": 1.25,
            }
        ],
        row={
            "latest_date": dt.datetime(2026, 5, 10, 15, 30, tzinfo=dt.UTC),
            "latest_source_updated_at": dt.datetime(2026, 5, 10, 15, 30, tzinfo=dt.UTC),
            "latest_occurred_at": dt.datetime(2026, 5, 10, 14, 0, tzinfo=dt.UTC),
            "document_count": 42,
        },
    )
    fake_slack = _FakeSlackClient()

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(company_context_client, "_load_slack_client", lambda: fake_slack)

    result = CompanyContextClient("postgresql://example").search(
        "ParadeDB BM25",
        limit=5,
        source="slack",
        source_type="slack_thread",
    )

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["indexed_count"] == 1
    assert result["live_count"] == 0
    assert result["indexed_cutoff"] == "2026-05-10T15:30:00+00:00"
    assert fake_slack.calls == [("ParadeDB BM25 after:2026-05-10", 5)]
    assert result["results"][0] == {
        "document_id": "slack:thread:C123:1770000000.000000",
        "source": "slack",
        "source_type": "slack_thread",
        "source_document_id": "",
        "source_chunk_id": "",
        "parent_document_id": None,
        "title": "BM25 indexing plan",
        "url": "https://slack.example/thread",
        "author_name": "",
        "access_scope": "",
        "score": 1.25,
        "preview": "",
        "lane": "indexed",
        "result_type": "slack_thread",
        "occurred_at": "2026-05-08T12:00:00+00:00",
        "source_updated_at": "2026-05-08T12:05:00+00:00",
        "metadata": {"channel_name": "eng-ai", "thread_ts": "1770000000.000000"},
    }
    query, args = fake.fetch_calls[0]
    assert "title ||| $1::text::pdb.boost(8) OR body ||| $1::text::pdb.boost(2)" in query
    assert "title ||| $2::text::pdb.boost(4) OR body ||| $2" in query
    assert "title ||| $3::text::pdb.boost(4) OR body ||| $3" in query
    assert ") OR (" in query
    assert "WHEN 'slack_thread' THEN 1.25" in query
    assert "WHEN 'slack_channel_day' THEN 0.75" in query
    assert "END DESC" in query
    assert "paradedb.score(document_id)" in query
    assert args == ("ParadeDB BM25", "ParadeDB", "BM25", "slack", "slack_thread", None, 5)
    assert fake.closed is True


def test_search_appends_live_slack_gap_results(monkeypatch):
    fake = _FakeConnection(
        rows=[],
        row={
            "latest_date": dt.datetime(2026, 5, 10, 15, 30, tzinfo=dt.UTC),
            "latest_source_updated_at": dt.datetime(2026, 5, 10, 15, 30, tzinfo=dt.UTC),
            "latest_occurred_at": dt.datetime(2026, 5, 10, 14, 0, tzinfo=dt.UTC),
            "document_count": 42,
        },
    )
    fake_slack = _FakeSlackClient(
        [
            {
                "channel": "eng-ai",
                "channel_id": "C123",
                "user": "alice",
                "user_id": "U123",
                "text": "New state root mismatch update",
                "timestamp": "1770000000.000000",
                "permalink": "https://slack.example/archives/C123/p1770000000000000",
                "thread_ts": "1770000000.000000",
                "reply_count": 3,
            }
        ]
    )

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(company_context_client, "_load_slack_client", lambda: fake_slack)

    result = CompanyContextClient("postgresql://example").search(
        "state root mismatch",
        limit=7,
        source="slack",
    )

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["indexed_count"] == 0
    assert result["live_count"] == 1
    assert result["indexed_cutoff"] == "2026-05-10T15:30:00+00:00"
    assert result["live_error"] is None
    assert fake_slack.calls == [("state root mismatch after:2026-05-10", 7)]
    assert result["results"] == [
        {
            "document_id": "",
            "source": "slack",
            "source_type": "slack_live_message",
            "source_document_id": "1770000000.000000",
            "source_chunk_id": "1770000000.000000",
            "parent_document_id": None,
            "title": "#eng-ai from alice",
            "url": "https://slack.example/archives/C123/p1770000000000000",
            "author_name": "alice",
            "access_scope": "",
            "score": None,
            "preview": "New state root mismatch update",
            "occurred_at": "2026-02-02T02:40:00+00:00",
            "source_updated_at": None,
            "lane": "live",
            "result_type": "slack_live_message",
            "metadata": {
                "channel_name": "eng-ai",
                "channel_id": "C123",
                "user_name": "alice",
                "user_id": "U123",
                "message_ts": "1770000000.000000",
                "thread_ts": "1770000000.000000",
                "reply_count": 3,
            },
        }
    ]


def test_search_preserves_existing_slack_after_modifier(monkeypatch):
    fake = _FakeConnection(
        rows=[],
        row={
            "latest_date": dt.datetime(2026, 5, 10, 15, 30, tzinfo=dt.UTC),
            "latest_source_updated_at": dt.datetime(2026, 5, 10, 15, 30, tzinfo=dt.UTC),
            "latest_occurred_at": dt.datetime(2026, 5, 10, 14, 0, tzinfo=dt.UTC),
            "document_count": 42,
        },
    )
    fake_slack = _FakeSlackClient()

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(company_context_client, "_load_slack_client", lambda: fake_slack)

    result = CompanyContextClient("postgresql://example").search(
        "state root after:2026-05-11",
        source="slack",
    )

    assert result["status"] == "ok"
    assert fake_slack.calls == [("state root after:2026-05-11", 10)]


def test_search_uses_or_terms_and_drops_stop_words(monkeypatch):
    fake = _FakeConnection(rows=[])

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").search(
        "what is the state root state mismatch in prod",
        limit=3,
    )

    assert result["status"] == "ok"
    query, args = fake.fetch_calls[0]
    assert "WHERE (title ||| $1::text::pdb.boost(8) OR body ||| $1::text::pdb.boost(2))" in query
    assert "OR (title ||| $2::text::pdb.boost(4) OR body ||| $2)" in query
    assert "OR (title ||| $3::text::pdb.boost(4) OR body ||| $3)" in query
    assert "OR (title ||| $4::text::pdb.boost(4) OR body ||| $4)" in query
    assert "OR (title ||| $5::text::pdb.boost(4) OR body ||| $5)" in query
    assert "title ||| $6::text::pdb.boost(4)" not in query
    assert args == (
        "what is the state root state mismatch in prod",
        "state",
        "root",
        "mismatch",
        "prod",
        None,
        None,
        None,
        3,
    )


def test_query_slack_messages_filters_user_and_orders_newest(monkeypatch):
    occurred_at = dt.datetime(2026, 5, 10, 14, 0, tzinfo=dt.UTC)
    updated_at = dt.datetime(2026, 5, 10, 14, 1, tzinfo=dt.UTC)
    fake = _FakeConnection(
        rows=[
            {
                "channel_id": "C123",
                "channel_name": "hello-world",
                "message_ts": "1770000000.000000",
                "occurred_at": occurred_at,
                "thread_ts": "1770000000.000000",
                "parent_message_ts": None,
                "is_thread_root": True,
                "user_id": "U_LUKE",
                "user_name": "dunshea.luke",
                "real_name": "Luke",
                "display_name": "Luke",
                "bot_id": "",
                "text": "latest workspace-wide context",
                "permalink": "https://slack.example/archives/C123/p1770000000000000",
                "reply_count": 0,
                "updated_at": updated_at,
                "score": None,
            }
        ],
    )

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").query_slack_messages(
        user="Luke",
        order="newest",
        limit=1,
    )

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["filters"] == {
        "channel": None,
        "user": "Luke",
        "thread_ts": None,
        "before": None,
        "after": None,
        "order": "newest",
    }
    assert result["results"] == [
        {
            "source": "slack",
            "source_type": "slack_message",
            "channel_id": "C123",
            "channel_name": "hello-world",
            "user_id": "U_LUKE",
            "user_name": "Luke",
            "bot_id": "",
            "message_ts": "1770000000.000000",
            "thread_ts": "1770000000.000000",
            "parent_message_ts": None,
            "is_thread_root": True,
            "text": "latest workspace-wide context",
            "permalink": "https://slack.example/archives/C123/p1770000000000000",
            "reply_count": 0,
            "occurred_at": "2026-05-10T14:00:00+00:00",
            "updated_at": "2026-05-10T14:01:00+00:00",
            "score": None,
        }
    ]
    query, args = fake.fetch_calls[0]
    assert "FROM slack_sync_messages m" in query
    assert "m.user_id = $1" in query
    assert "lower(coalesce(u.real_name, '')) = lower($1)" in query
    assert "ORDER BY m.occurred_at DESC NULLS LAST, m.message_ts DESC" in query
    assert "LIMIT $2" in query
    assert args == ("Luke", 1)
    assert fake.closed is True


def test_query_slack_messages_searches_text_channel_time_and_relevance(monkeypatch):
    fake = _FakeConnection(rows=[])

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").query_slack_messages(
        query="Centaur Slack memory",
        channel="#intendo",
        after="2026-05-01T00:00:00Z",
        before="1780303286.801179",
        limit=12,
    )

    assert result["status"] == "ok"
    assert result["filters"] == {
        "channel": "intendo",
        "user": None,
        "thread_ts": None,
        "before": "2026-06-01T08:41:26.801179+00:00",
        "after": "2026-05-01T00:00:00+00:00",
        "order": "relevance",
    }
    query, args = fake.fetch_calls[0]
    assert "websearch_to_tsquery('english', $1)" in query
    assert "lower(coalesce(c.channel_name, '')) = lower($2)" in query
    assert "m.occurred_at < $3" in query
    assert "m.occurred_at >= $4" in query
    assert "ORDER BY ts_rank_cd(" in query
    assert "LIMIT $5" in query
    assert args == (
        "Centaur Slack memory",
        "intendo",
        dt.datetime(2026, 6, 1, 8, 41, 26, 801179, tzinfo=dt.UTC),
        dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.UTC),
        12,
    )
    assert fake.closed is True


def test_query_slack_messages_rejects_bad_order(monkeypatch):
    fake = _FakeConnection(rows=[])

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").query_slack_messages(
        order="random",
    )

    assert result == {
        "status": "error",
        "error": "order must be one of: relevance, newest, oldest",
    }
    assert fake.fetch_calls == []
    assert fake.closed is True


def test_search_scopes_slack_documents_to_active_thread_channel(monkeypatch):
    fake = _FakeConnection(rows=[])

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setenv("CENTAUR_THREAD_KEY", "slack:T123:C_SCOPE:1778883099.579529")
    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").search(
        "channel only",
        source="slack",
        limit=4,
    )

    assert result["status"] == "ok"
    assert result["slack_scope"] == {
        "team_id": "T123",
        "channel_id": "C_SCOPE",
        "thread_ts": "1778883099.579529",
    }
    query, args = fake.fetch_calls[0]
    assert "OR metadata->>'channel_id' = $" in query
    assert args == ("channel only", "channel", "only", "slack", None, "C_SCOPE", 4)
    assert fake.closed is True


def test_query_slack_messages_scopes_to_active_thread_channel(monkeypatch):
    fake = _FakeConnection(rows=[])

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setenv("CENTAUR_THREAD_KEY", "slack:T123:C_SCOPE:1778883099.579529")
    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").query_slack_messages(
        user="Luke",
        order="newest",
        limit=2,
    )

    assert result["status"] == "ok"
    assert result["slack_scope"] == {
        "team_id": "T123",
        "channel_id": "C_SCOPE",
        "thread_ts": "1778883099.579529",
    }
    query, args = fake.fetch_calls[0]
    assert "m.channel_id = $1" in query
    assert "m.user_id = $2" in query
    assert "LIMIT $3" in query
    assert args == ("C_SCOPE", "Luke", 2)
    assert fake.closed is True


def test_read_document_rejects_slack_document_outside_active_channel(monkeypatch):
    fake = _FakeConnection(
        row={
            "document_id": "slack:channel_day:C_OTHER:2026-05-08",
            "source": "slack",
            "source_type": "slack_channel_day",
            "title": "#other - 2026-05-08",
            "body": "outside",
            "url": "",
            "occurred_at": None,
            "source_updated_at": None,
            "metadata": '{"channel_id": "C_OTHER", "channel_name": "other"}',
        }
    )

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setenv("CENTAUR_THREAD_KEY", "slack:T123:C_SCOPE:1778883099.579529")
    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").read_document(
        "slack:channel_day:C_OTHER:2026-05-08",
    )

    assert result == {
        "status": "error",
        "error": "document is outside the active Slack channel scope",
        "slack_scope": {
            "team_id": "T123",
            "channel_id": "C_SCOPE",
            "thread_ts": "1778883099.579529",
        },
    }
    assert fake.closed is True



def test_latest_date_returns_latest_indexed_slack_timestamp(monkeypatch):
    fake = _FakeConnection(
        row={
            "latest_date": dt.datetime(2026, 5, 10, 15, 30, tzinfo=dt.UTC),
            "latest_source_updated_at": dt.datetime(2026, 5, 10, 15, 30, tzinfo=dt.UTC),
            "latest_occurred_at": dt.datetime(2026, 5, 10, 14, 0, tzinfo=dt.UTC),
            "document_count": 42,
        }
    )

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").latest_date(
        source="slack",
        source_type="slack_thread",
    )

    assert result == {
        "status": "ok",
        "source": "slack",
        "source_type": "slack_thread",
        "document_count": 42,
        "latest_date": "2026-05-10T15:30:00+00:00",
        "latest_source_updated_at": "2026-05-10T15:30:00+00:00",
        "latest_occurred_at": "2026-05-10T14:00:00+00:00",
    }
    _, args = fake.fetchrow_calls[0]
    assert args == ("slack", "slack_thread", None)
    assert fake.closed is True


def test_latest_date_reports_empty_index(monkeypatch):
    fake = _FakeConnection(
        row={
            "latest_date": None,
            "latest_source_updated_at": None,
            "latest_occurred_at": None,
            "document_count": 0,
        }
    )

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").latest_date(source="slack")

    assert result == {
        "status": "ok",
        "source": "slack",
        "source_type": None,
        "document_count": 0,
        "latest_date": None,
        "latest_source_updated_at": None,
        "latest_occurred_at": None,
    }
    assert fake.closed is True


def test_read_document_returns_full_content_by_default(monkeypatch):
    body = "x" * 2_500
    fake = _FakeConnection(
        row={
            "document_id": "slack:channel_day:C123:2026-05-08",
            "source": "slack",
            "source_type": "slack_channel_day",
            "title": "#eng-ai - 2026-05-08",
            "body": body,
            "url": "",
            "occurred_at": None,
            "source_updated_at": None,
            "metadata": '{"channel_name": "eng-ai"}',
        }
    )

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").read_document(
        " slack:channel_day:C123:2026-05-08 ",
    )

    assert result["status"] == "ok"
    assert result["document_id"] == "slack:channel_day:C123:2026-05-08"
    assert result["chars"] == 2_500
    assert result["total_chars"] == 2_500
    assert result["truncated"] is False
    assert result["content"] == body
    assert result["metadata"] == {"channel_name": "eng-ai"}
    _, args = fake.fetchrow_calls[0]
    assert args == ("slack:channel_day:C123:2026-05-08",)
    assert fake.closed is True


def test_read_document_can_return_bounded_content(monkeypatch):
    body = "x" * 2_500
    fake = _FakeConnection(
        row={
            "document_id": "slack:channel_day:C123:2026-05-08",
            "source": "slack",
            "source_type": "slack_channel_day",
            "title": "#eng-ai - 2026-05-08",
            "body": body,
            "url": "",
            "occurred_at": None,
            "source_updated_at": None,
            "metadata": '{"channel_name": "eng-ai"}',
        }
    )

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").read_document(
        "slack:channel_day:C123:2026-05-08",
        max_chars=1_200,
    )

    assert result["status"] == "ok"
    assert result["document_id"] == "slack:channel_day:C123:2026-05-08"
    assert result["chars"] == 1_200
    assert result["total_chars"] == 2_500
    assert result["truncated"] is True
    assert result["content"] == "x" * 1_200
    assert result["metadata"] == {"channel_name": "eng-ai"}
    _, args = fake.fetchrow_calls[0]
    assert args == ("slack:channel_day:C123:2026-05-08",)
    assert fake.closed is True


def test_read_document_reports_missing_document(monkeypatch):
    fake = _FakeConnection(row=None)

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").read_document("missing-doc")

    assert result == {"status": "error", "error": "document not found: missing-doc"}
