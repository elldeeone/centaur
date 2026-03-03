from __future__ import annotations

import re
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from toon_format import encode as toon_encode

from api.deps import EmbeddingService, get_embedding_service, get_pool, verify_api_key
from shared.tool_manager import _flatten_for_tabular

router = APIRouter(prefix="/api/search", dependencies=[Depends(verify_api_key)])

DISALLOWED_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


class SearchRequest(BaseModel):
    query: str
    sources: list[str] | None = None
    limit: int = Field(default=20, ge=1, le=100)


class SearchResult(BaseModel):
    score: float
    source: str
    kind: str
    source_id: str
    content: str
    metadata: dict
    url: str | None = None


class SqlQueryRequest(BaseModel):
    query: str


@router.post("")
async def search(
    request: Request,
    body: SearchRequest,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    embedding_svc: Annotated[EmbeddingService, Depends(get_embedding_service)],
):
    """Hybrid semantic + keyword search via pgvector + tsvector on embeddings table."""
    embedding = await embedding_svc.embed(body.query)
    embedding_literal = "[" + ",".join(str(v) for v in embedding) + "]"

    source_filter = ""
    args: list = [body.query, body.limit]
    if body.sources:
        placeholders = ",".join(f"${i + 3}" for i in range(len(body.sources)))
        source_filter = f"AND source IN ({placeholders})"
        args.extend(body.sources)

    sql = f"""
    WITH vector_results AS (
        SELECT id, source, kind, source_id, content, metadata,
               ROW_NUMBER() OVER (
                   ORDER BY embedding <=> '{embedding_literal}'::vector
               ) AS vector_rank
        FROM embeddings
        WHERE TRUE {source_filter}
        ORDER BY embedding <=> '{embedding_literal}'::vector
        LIMIT $2
    ),
    fts_results AS (
        SELECT id, source, kind, source_id, content, metadata,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', $1)) DESC
               ) AS fts_rank
        FROM embeddings
        WHERE tsv @@ plainto_tsquery('english', $1) {source_filter}
        LIMIT $2
    ),
    combined AS (
        SELECT
            COALESCE(v.id, f.id) AS id,
            COALESCE(v.source, f.source) AS source,
            COALESCE(v.kind, f.kind) AS kind,
            COALESCE(v.source_id, f.source_id) AS source_id,
            COALESCE(v.content, f.content) AS content,
            COALESCE(v.metadata, f.metadata) AS metadata,
            COALESCE(1.0 / (60 + v.vector_rank), 0) +
            COALESCE(1.0 / (60 + f.fts_rank), 0) AS rrf_score
        FROM vector_results v
        FULL OUTER JOIN fts_results f ON v.id = f.id
    )
    SELECT source, kind, source_id, content, metadata, rrf_score AS score
    FROM combined
    ORDER BY rrf_score DESC
    LIMIT $2
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    results = [
        SearchResult(
            score=float(r["score"]),
            source=r["source"],
            kind=r["kind"],
            source_id=r["source_id"],
            content=r["content"][:500],
            metadata=dict(r["metadata"]) if r["metadata"] else {},
        )
        for r in rows
    ]
    if "text/plain" in request.headers.get("accept", ""):
        return PlainTextResponse(
            toon_encode(_flatten_for_tabular([r.model_dump() for r in results]))
        )
    return results


@router.post("/sql")
async def sql_query(
    request: Request,
    body: SqlQueryRequest,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
):
    """Run a read-only SQL query against raw_records or embeddings."""
    if DISALLOWED_SQL.search(body.query):
        raise HTTPException(status_code=400, detail="Only read-only queries are allowed")

    async with pool.acquire() as conn:
        tr = conn.transaction(readonly=True)
        await tr.start()
        try:
            rows = await conn.fetch(body.query)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            await tr.rollback()

    results = [dict(r) for r in rows]
    if "text/plain" in request.headers.get("accept", ""):
        return PlainTextResponse(toon_encode(_flatten_for_tabular(results)))
    return results


@router.get("/sources")
async def list_sources(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> list[dict]:
    """Record counts per source from raw_records."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source, COUNT(*) AS record_count,
                   COUNT(DISTINCT kind) AS kind_count
            FROM raw_records
            GROUP BY source
            ORDER BY source
            """
        )
    return [dict(r) for r in rows]
