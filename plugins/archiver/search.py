#!/usr/bin/env python3
"""Search adapter for parchiver with hybrid search support."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from .db import get_db_connection
from .telemetry import get_logger, step_timer


load_dotenv()

OPENROUTER_API_KEY = os.getenv("PARCHIVER_OPENROUTER_API_KEY")
OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
EMBEDDING_MODEL = "openai/text-embedding-3-large"
EMBEDDING_DIMS = int(os.getenv("PARCHIVER_EMBEDDING_DIMS", "1024"))
logger = get_logger(__name__)


@dataclass
class SearchResult:
    chunk_id: int
    embedding_id: int
    page: int
    chunk_index: int
    text: str
    score: float
    source: str  # "dense", "sparse", or "hybrid"
    file_hash: Optional[str] = None
    filename: Optional[str] = None
    company_name: Optional[str] = None
    source_metadata: Optional[dict] = None
    file_metadata: Optional[dict] = None
    is_preamble: bool = False


def _extract_company_name(source_metadata: Optional[dict]) -> Optional[str]:
    if not source_metadata or not isinstance(source_metadata, dict):
        return None
    reducto = source_metadata.get("reducto")
    if isinstance(reducto, dict):
        company = reducto.get("company")
        if isinstance(company, dict) and company.get("name"):
            return company["name"]
    return source_metadata.get("company_hint")


def _generate_query_embedding(query: str) -> list[float]:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    with step_timer(logger, "search.generate_query_embedding", query_len=len(query)):
        response = requests.post(
            OPENROUTER_EMBEDDINGS_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": EMBEDDING_MODEL,
                "input": [query],
                "dimensions": EMBEDDING_DIMS,
            },
        )
        response.raise_for_status()
        return response.json()["data"][0]["embedding"]


def _dense_search(
    cur,
    query_embedding: list[float],
    limit: int,
    threshold: float,
) -> list[SearchResult]:
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    cur.execute(
        """
        SELECT
            c.id,
            c.embedding_id,
            c.page,
            c.chunk_index,
            c.text,
            1 - (c.embedding <=> %s::vector) as similarity,
            f.file_hash,
            f.filename,
            (SELECT s.metadata FROM file_sources fs JOIN sources s ON fs.source_id = s.id
             WHERE fs.file_id = f.id ORDER BY fs.created_at DESC LIMIT 1) as source_metadata,
            (SELECT fs.metadata FROM file_sources fs
             WHERE fs.file_id = f.id ORDER BY fs.created_at DESC LIMIT 1) as file_metadata
        FROM chunks c
        JOIN embeddings e ON c.embedding_id = e.id
        LEFT JOIN parses p ON e.content_hash = p.content_hash
        LEFT JOIN files f ON p.file_id = f.id
        WHERE c.embedding IS NOT NULL
          AND 1 - (c.embedding <=> %s::vector) > %s
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
        """,
        (embedding_str, embedding_str, threshold, embedding_str, limit),
    )

    results = []
    for row in cur.fetchall():
        import json as _json
        src_meta = row[8] if isinstance(row[8], dict) else (_json.loads(row[8]) if row[8] else None)
        file_meta = row[9] if isinstance(row[9], dict) else (_json.loads(row[9]) if row[9] else None)
        results.append(
            SearchResult(
                chunk_id=row[0],
                embedding_id=row[1],
                page=row[2],
                chunk_index=row[3],
                text=row[4],
                score=float(row[5]),
                source="dense",
                file_hash=row[6],
                filename=row[7],
                company_name=_extract_company_name(src_meta),
                source_metadata=src_meta,
                file_metadata=file_meta,
                is_preamble=row[3] == -1,
            )
        )
    return results


def _sparse_search(
    cur,
    query: str,
    limit: int,
) -> list[SearchResult]:
    cleaned = re.sub(r"[^\w\s]", " ", query).strip()
    terms = cleaned.split()
    if not terms:
        return []

    ts_query = " | ".join(terms)

    cur.execute(
        """
        SELECT
            c.id,
            c.embedding_id,
            c.page,
            c.chunk_index,
            c.text,
            ts_rank(c.text_search_tsv, to_tsquery('english', %s)) as rank,
            f.file_hash,
            f.filename,
            (SELECT s.metadata FROM file_sources fs JOIN sources s ON fs.source_id = s.id
             WHERE fs.file_id = f.id ORDER BY fs.created_at DESC LIMIT 1) as source_metadata,
            (SELECT fs.metadata FROM file_sources fs
             WHERE fs.file_id = f.id ORDER BY fs.created_at DESC LIMIT 1) as file_metadata
        FROM chunks c
        JOIN embeddings e ON c.embedding_id = e.id
        LEFT JOIN parses p ON e.content_hash = p.content_hash
        LEFT JOIN files f ON p.file_id = f.id
        WHERE c.text_search_tsv @@ to_tsquery('english', %s)
        ORDER BY rank DESC
        LIMIT %s
        """,
        (ts_query, ts_query, limit),
    )

    results = []
    for row in cur.fetchall():
        import json as _json
        src_meta = row[8] if isinstance(row[8], dict) else (_json.loads(row[8]) if row[8] else None)
        file_meta = row[9] if isinstance(row[9], dict) else (_json.loads(row[9]) if row[9] else None)
        results.append(
            SearchResult(
                chunk_id=row[0],
                embedding_id=row[1],
                page=row[2],
                chunk_index=row[3],
                text=row[4],
                score=float(row[5]),
                source="sparse",
                file_hash=row[6],
                filename=row[7],
                company_name=_extract_company_name(src_meta),
                source_metadata=src_meta,
                file_metadata=file_meta,
                is_preamble=row[3] == -1,
            )
        )
    return results


def _deduplicate_results(
    dense_results: list[SearchResult],
    sparse_results: list[SearchResult],
) -> list[SearchResult]:
    seen: dict[int, SearchResult] = {}

    for r in dense_results:
        r.source = "hybrid"
        seen[r.chunk_id] = r

    for r in sparse_results:
        if r.chunk_id not in seen:
            r.source = "hybrid"
            seen[r.chunk_id] = r

    return list(seen.values())


def search(
    query: str,
    limit: int = 10,
    mode: str = "hybrid",
    threshold: float = 0.3,
    sparse_limit: Optional[int] = None,
) -> dict:
    """
    Search indexed documents.

    Args:
        query: Search query text
        limit: Max results to return
        mode: "hybrid" (default), "dense" (vector only), "sparse" (BM25 only)
        threshold: Similarity threshold for dense search (0-1)
        sparse_limit: Number of sparse results to fetch (defaults to limit)
    """
    with step_timer(
        logger,
        "search.query",
        mode=mode,
        limit=limit,
        threshold=threshold,
        query_len=len(query),
    ) as step:
        conn = get_db_connection()
        cur = conn.cursor()

        dense_results: list[SearchResult] = []
        sparse_results: list[SearchResult] = []

        if mode in ("hybrid", "dense"):
            query_embedding = _generate_query_embedding(query)
            dense_results = _dense_search(cur, query_embedding, limit, threshold)

        if mode in ("hybrid", "sparse"):
            sparse_results = _sparse_search(cur, query, sparse_limit or limit)

        if mode == "hybrid":
            all_results = _deduplicate_results(dense_results, sparse_results)
            all_results = sorted(all_results, key=lambda r: r.score, reverse=True)[:limit]
        elif mode == "dense":
            all_results = dense_results
        else:
            all_results = sparse_results

        cur.close()
        conn.close()
        step.set(
            dense_count=len(dense_results),
            sparse_count=len(sparse_results),
            result_count=len(all_results),
        )
        return {
            "status": "ok",
            "query": query,
            "mode": mode,
            "count": len(all_results),
            "results": [
                {
                    "chunk_id": r.chunk_id,
                    "embedding_id": r.embedding_id,
                    "page": r.page,
                    "chunk_index": r.chunk_index,
                    "text": r.text[:500] + "..." if len(r.text) > 500 else r.text,
                    "score": round(r.score, 4),
                    "source": r.source,
                    "file_hash": r.file_hash,
                    "filename": r.filename,
                    "company_name": r.company_name,
                    "is_preamble": r.is_preamble,
                }
                for r in all_results
            ],
        }


def search_stats() -> dict:
    """Get search index statistics."""
    with step_timer(logger, "search.stats"):
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM chunks;")
        total_chunks = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL;")
        embedded_chunks = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM embeddings;")
        total_embeddings = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM files;")
        total_files = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM sources;")
        total_sources = cur.fetchone()[0]

        cur.close()
        conn.close()

        return {
            "status": "ok",
            "stats": {
                "chunks": total_chunks,
                "embedded_chunks": embedded_chunks,
                "embeddings": total_embeddings,
                "files": total_files,
                "sources": total_sources,
            },
        }
