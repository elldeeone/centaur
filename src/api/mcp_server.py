from __future__ import annotations

import json
import re
from typing import Any

import asyncpg
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from toon_format import encode as toon_encode

from api.deps import EmbeddingService
from shared.plugin_manager import PluginManager

mcp = FastMCP(
    "Tempo AI v2",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)  # mounted at /mcp in app.py

_pool: asyncpg.Pool | None = None
_plugin_manager: PluginManager | None = None

DISALLOWED_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def set_pool(pool: asyncpg.Pool) -> None:
    global _pool
    _pool = pool


def set_plugin_manager(plugin_manager: PluginManager) -> None:
    global _plugin_manager
    _plugin_manager = plugin_manager


def _get_plugin_manager() -> PluginManager:
    if _plugin_manager is None:
        raise RuntimeError("Plugin manager not initialized")
    return _plugin_manager


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
async def list_plugins() -> str:
    """List all available plugins and their tool names. Call this first to discover
    what plugins and tools are available, then use describe_plugin to get full
    method schemas before calling call_plugin."""
    manager = _get_plugin_manager()
    return _to_toon(manager.list_plugins())


@mcp.tool()
async def describe_plugin(plugin: str) -> str:
    """Get full method schemas (parameters, types, defaults) for a plugin's tools.
    Call this before call_plugin to know the exact arguments a tool expects."""
    manager = _get_plugin_manager()
    return _to_toon(manager.describe_plugin(plugin))


@mcp.tool()
async def call_plugin(plugin: str, tool: str, args: dict | None = None) -> str:
    """Call a plugin tool. Use list_plugins to discover plugins, describe_plugin
    to get method schemas, then call this with the plugin name, tool name, and
    a dict of arguments."""
    manager = _get_plugin_manager()
    return await manager.call_tool(plugin, tool, args or {})
