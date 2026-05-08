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
        ]
    )

    async def fake_connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(company_context_client.asyncpg, "connect", fake_connect)

    result = CompanyContextClient("postgresql://example").search(
        "ParadeDB BM25",
        limit=5,
        source="slack",
        source_type="slack_thread",
    )

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["results"][0] == {
        "document_id": "slack:thread:C123:1770000000.000000",
        "source": "slack",
        "source_type": "slack_thread",
        "title": "BM25 indexing plan",
        "url": "https://slack.example/thread",
        "score": 1.25,
        "occurred_at": "2026-05-08T12:00:00+00:00",
        "source_updated_at": "2026-05-08T12:05:00+00:00",
        "metadata": {"channel_name": "eng-ai", "thread_ts": "1770000000.000000"},
    }
    query, args = fake.fetch_calls[0]
    assert "title ||| $1 OR body ||| $1" in query
    assert "paradedb.score(document_id)" in query
    assert args == ("ParadeDB BM25", "slack", "slack_thread", 5)
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
