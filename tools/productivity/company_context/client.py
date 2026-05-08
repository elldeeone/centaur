"""Search company context documents stored in Postgres."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Any

import asyncpg

from centaur_sdk.tool_sdk import secret

DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50


def _clamp(value: int, *, minimum: int, maximum: int) -> int:
    """Clamp integer tool inputs to predictable output bounds."""
    return max(minimum, min(int(value), maximum))


def _as_dict(value: Any) -> dict[str, Any]:
    """Decode asyncpg JSON/JSONB values into a dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _isoformat(value: Any) -> str | None:
    """Serialize datetimes while leaving absent values explicit."""
    if isinstance(value, datetime):
        return value.isoformat()
    return None


class CompanyContextClient:
    """Query the shared company context document table."""

    def __init__(self, database_url: str | None = None) -> None:
        # DATABASE_URL is owned by the API process, not an agent-facing secret.
        env_database_url = os.getenv("DATABASE_URL")  # noqa: TID251
        self._database_url = (
            database_url or env_database_url or secret("DATABASE_URL", default="")
        ).strip()

    def _require_database_url(self) -> str:
        if not self._database_url:
            raise RuntimeError("DATABASE_URL is required for company context search")
        return self._database_url

    async def _connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(self._require_database_url(), command_timeout=30)

    async def _search_async(
        self,
        *,
        query: str,
        limit: int,
        source: str | None,
        source_type: str | None,
    ) -> dict[str, Any]:
        conn = await self._connect()
        try:
            rows = await conn.fetch(
                """
                SELECT
                    document_id,
                    source,
                    source_type,
                    title,
                    url,
                    occurred_at,
                    source_updated_at,
                    metadata,
                    paradedb.score(document_id) AS score
                FROM company_context_documents
                WHERE (title ||| $1 OR body ||| $1)
                  AND ($2::text IS NULL OR source = $2)
                  AND ($3::text IS NULL OR source_type = $3)
                ORDER BY paradedb.score(document_id), source_updated_at DESC NULLS LAST
                LIMIT $4
                """,
                query,
                source,
                source_type,
                limit,
            )
            results = []
            for row in rows:
                results.append(
                    {
                        "document_id": str(row["document_id"]),
                        "source": str(row["source"]),
                        "source_type": str(row["source_type"]),
                        "title": str(row["title"] or ""),
                        "url": str(row["url"] or ""),
                        "score": float(row["score"] or 0.0),
                        "occurred_at": _isoformat(row["occurred_at"]),
                        "source_updated_at": _isoformat(row["source_updated_at"]),
                        "metadata": _as_dict(row["metadata"]),
                    }
                )
            return {
                "status": "ok",
                "query": query,
                "source": source,
                "source_type": source_type,
                "count": len(results),
                "results": results,
            }
        finally:
            await conn.close()

    def search(
        self,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        source: str | None = None,
        source_type: str | None = None,
    ) -> dict:
        """Search company context documents and return candidate document ids."""
        normalized_query = query.strip()
        if not normalized_query:
            return {"status": "error", "error": "query cannot be empty"}

        try:
            return asyncio.run(
                self._search_async(
                    query=normalized_query,
                    limit=_clamp(limit, minimum=1, maximum=MAX_SEARCH_LIMIT),
                    source=source.strip() if source else None,
                    source_type=source_type.strip() if source_type else None,
                )
            )
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def _read_document_async(self, document_id: str, max_chars: int | None) -> dict[str, Any]:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                """
                SELECT
                    document_id,
                    source,
                    source_type,
                    title,
                    body,
                    url,
                    occurred_at,
                    source_updated_at,
                    metadata
                FROM company_context_documents
                WHERE document_id = $1
                """,
                document_id,
            )
            if not row:
                return {
                    "status": "error",
                    "error": f"document not found: {document_id}",
                }

            body = str(row["body"] or "")
            content = body if max_chars is None else body[:max_chars]
            truncated = max_chars is not None and len(body) > max_chars
            return {
                "status": "ok",
                "document_id": str(row["document_id"]),
                "source": str(row["source"]),
                "source_type": str(row["source_type"]),
                "title": str(row["title"] or ""),
                "url": str(row["url"] or ""),
                "occurred_at": _isoformat(row["occurred_at"]),
                "source_updated_at": _isoformat(row["source_updated_at"]),
                "metadata": _as_dict(row["metadata"]),
                "chars": len(content),
                "total_chars": len(body),
                "truncated": truncated,
                "content": content,
            }
        finally:
            await conn.close()

    def read_document(self, document_id: str, max_chars: int = 0) -> dict:
        """Read a company context document by id, returning full content by default."""
        normalized_document_id = document_id.strip()
        if not normalized_document_id:
            return {"status": "error", "error": "document_id cannot be empty"}

        try:
            return asyncio.run(
                self._read_document_async(
                    document_id=normalized_document_id,
                    max_chars=max_chars if max_chars > 0 else None,
                )
            )
        except Exception as exc:
            return {"status": "error", "error": str(exc)}


def _client() -> CompanyContextClient:
    return CompanyContextClient()
