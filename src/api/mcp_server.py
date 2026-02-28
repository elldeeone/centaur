from __future__ import annotations

import json
import re
from typing import Any

import asyncpg
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from toon_format import encode as toon_encode

from api.deps import EmbeddingService
from shared.tool_manager import ToolManager

mcp = FastMCP(
    "Tempo AI v2",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)  # mounted at /mcp in app.py

_pool: asyncpg.Pool | None = None
_tool_manager: ToolManager | None = None

DISALLOWED_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def set_pool(pool: asyncpg.Pool) -> None:
    global _pool
    _pool = pool


def set_tool_manager(tool_manager: ToolManager) -> None:
    global _tool_manager
    _tool_manager = tool_manager


def _get_tool_manager() -> ToolManager:
    if _tool_manager is None:
        raise RuntimeError("Tool manager not initialized")
    return _tool_manager


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized")
    return _pool


def _to_toon(data: Any) -> str:
    """Encode data as TOON for token-efficient LLM responses."""
    try:
        return toon_encode(data)
    except Exception:
        return json.dumps(data, default=str)


def _serialize(rows: list[asyncpg.Record]) -> str:
    return _to_toon([dict(r) for r in rows])


@mcp.tool()
async def search(query: str, sources: list[str] | None = None, limit: int = 20) -> str:
    """Hybrid semantic + keyword search across all ingested data."""
    pool = _get_pool()
    svc = EmbeddingService(pool=pool)
    embedding = await svc.embed(query)
    embedding_literal = "[" + ",".join(str(v) for v in embedding) + "]"

    source_filter = ""
    args: list[Any] = [query, limit]
    if sources:
        placeholders = ",".join(f"${i + 3}" for i in range(len(sources)))
        source_filter = f"AND source IN ({placeholders})"
        args.extend(sources)

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
    return _serialize(rows)


@mcp.tool()
async def sql_query(query: str) -> str:
    """Run a read-only SQL query against raw_records (JSONB) or embeddings."""
    if DISALLOWED_SQL.search(query):
        return json.dumps({"error": "Only read-only queries are allowed"})

    pool = _get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(query)
        except Exception as e:
            return json.dumps({"error": str(e)})
    return _serialize(rows)


@mcp.tool()
async def list_tools() -> str:
    """List all available tools and their methods. Call this first to discover
    what tools are available, then use describe_tool to get full method schemas
    before calling call_tool."""
    manager = _get_tool_manager()
    return _to_toon(manager.list_tools())


@mcp.tool()
async def describe_tool(tool: str) -> str:
    """Get full method schemas (parameters, types, defaults) for a tool's methods.
    Call this before call_tool to know the exact arguments a method expects."""
    manager = _get_tool_manager()
    return _to_toon(manager.describe_tool(tool))


@mcp.tool()
async def call_tool(tool: str, method: str, args: dict | None = None) -> str:
    """Call a tool method. Use list_tools to discover tools, describe_tool
    to get method schemas, then call this with the tool name, method name,
    and a dict of arguments."""
    manager = _get_tool_manager()
    return await manager.call_tool(tool, method, args or {})
