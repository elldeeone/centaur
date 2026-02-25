#!/usr/bin/env python3
"""
Parchiver database helpers and schema initialization.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import psycopg2
from dotenv import load_dotenv

from .telemetry import get_logger, step_timer


load_dotenv()

DATABASE_URL = os.getenv("PARCHIVER_DATABASE_URL")
PARCHIVER_SCHEMA = "parchiver"
PARCHIVER_EMBEDDING_DIMS = int(os.getenv("PARCHIVER_EMBEDDING_DIMS", "3072"))
logger = get_logger(__name__)


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("PARCHIVER_DATABASE_URL not set")
    with step_timer(logger, "db.connect", schema=PARCHIVER_SCHEMA):
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {PARCHIVER_SCHEMA};")
            cur.execute(f"SET search_path TO {PARCHIVER_SCHEMA}, public;")
        conn.autocommit = False
        return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sources (
            id BIGSERIAL PRIMARY KEY,
            source_type TEXT NOT NULL,
            canonical_url TEXT,
            raw_url TEXT,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS sources_type_canonical_url_uidx
        ON sources (source_type, canonical_url)
        WHERE canonical_url IS NOT NULL;
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id BIGSERIAL PRIMARY KEY,
            file_hash TEXT NOT NULL,
            filename TEXT NOT NULL,
            mime_type TEXT,
            size_bytes BIGINT,
            local_path TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS files_file_hash_uidx
        ON files (file_hash);
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS file_sources (
            id BIGSERIAL PRIMARY KEY,
            file_id BIGINT REFERENCES files(id) ON DELETE CASCADE,
            source_id BIGINT REFERENCES sources(id) ON DELETE CASCADE,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )
    # Migration: add metadata column if table already exists without it
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'file_sources'
                  AND column_name = 'metadata'
            ) THEN
                ALTER TABLE file_sources ADD COLUMN metadata JSONB DEFAULT '{}'::jsonb;
            END IF;
        END $$;
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS file_sources_file_source_uidx
        ON file_sources (file_id, source_id);
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS sources_metadata_gin_idx
        ON sources USING gin (metadata);
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS file_sources_metadata_gin_idx
        ON file_sources USING gin (metadata);
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS parses (
            id BIGSERIAL PRIMARY KEY,
            file_id BIGINT REFERENCES files(id) ON DELETE CASCADE,
            reducto_file_id TEXT,
            parse_job_id TEXT,
            extract_job_id TEXT,
            parse_json JSONB,
            extract_json JSONB,
            parsed_text TEXT,
            content_hash TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS parses_content_hash_idx
        ON parses (content_hash);
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
            id BIGSERIAL PRIMARY KEY,
            content_hash TEXT,
            model TEXT NOT NULL,
            dims INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS embeddings_fingerprint_uidx
        ON embeddings (fingerprint);
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS embeddings_content_hash_idx
        ON embeddings (content_hash);
        """
    )

    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS chunks (
            id BIGSERIAL PRIMARY KEY,
            embedding_id BIGINT REFERENCES embeddings(id) ON DELETE CASCADE,
            page INTEGER,
            chunk_index INTEGER,
            text TEXT NOT NULL,
            token_count INTEGER,
            embedding vector({PARCHIVER_EMBEDDING_DIMS})
        );
        """
    )
    if PARCHIVER_EMBEDDING_DIMS <= 2000:
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
            ON chunks USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
            """
        )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS chunks_embedding_id_idx
        ON chunks (embedding_id);
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS archives (
            id BIGSERIAL PRIMARY KEY,
            file_id BIGINT REFERENCES files(id) ON DELETE CASCADE,
            r2_bucket TEXT NOT NULL,
            r2_key TEXT NOT NULL,
            etag TEXT,
            size_bytes BIGINT,
            company_slug TEXT,
            archived_at TIMESTAMP DEFAULT NOW()
        );
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS archives_r2_key_uidx
        ON archives (r2_bucket, r2_key);
        """
    )

    conn.commit()
    cur.close()
    conn.close()


def fetch_one(cur, query: str, params: tuple[Any, ...]) -> Optional[dict]:
    cur.execute(query, params)
    row = cur.fetchone()
    if not row:
        return None
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))


def get_source(cur, source_type: str, canonical_url: Optional[str]) -> Optional[dict]:
    if canonical_url is None:
        return None
    return fetch_one(
        cur,
        """
        SELECT * FROM sources
        WHERE source_type = %s AND canonical_url = %s
        """,
        (source_type, canonical_url),
    )


def create_source(cur, source_type: str, canonical_url: Optional[str], raw_url: Optional[str], metadata: dict) -> dict:
    import json
    cur.execute(
        """
        INSERT INTO sources (source_type, canonical_url, raw_url, metadata)
        VALUES (%s, %s, %s, %s)
        RETURNING *
        """,
        (source_type, canonical_url, raw_url, json.dumps(metadata)),
    )
    row = cur.fetchone()
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))


def get_file_by_hash(cur, file_hash: str) -> Optional[dict]:
    return fetch_one(
        cur,
        """
        SELECT * FROM files
        WHERE file_hash = %s
        """,
        (file_hash,),
    )


def create_file(
    cur,
    file_hash: str,
    filename: str,
    mime_type: Optional[str],
    size_bytes: Optional[int],
    local_path: Optional[str],
) -> dict:
    cur.execute(
        """
        INSERT INTO files (file_hash, filename, mime_type, size_bytes, local_path)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
        """,
        (file_hash, filename, mime_type, size_bytes, local_path),
    )
    row = cur.fetchone()
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))


def link_file_source(cur, file_id: int, source_id: int, metadata: dict | None = None) -> dict:
    import json
    meta_json = json.dumps(metadata) if metadata else "{}"
    cur.execute(
        """
        INSERT INTO file_sources (file_id, source_id, metadata)
        VALUES (%s, %s, %s)
        ON CONFLICT (file_id, source_id)
        DO UPDATE SET metadata = file_sources.metadata || EXCLUDED.metadata
        RETURNING *
        """,
        (file_id, source_id, meta_json),
    )
    row = cur.fetchone()
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))


def get_parse_by_content_hash(cur, content_hash: str) -> Optional[dict]:
    return fetch_one(
        cur,
        """
        SELECT * FROM parses
        WHERE content_hash = %s
        """,
        (content_hash,),
    )


def get_parse_by_file_id(cur, file_id: int) -> Optional[dict]:
    return fetch_one(
        cur,
        """
        SELECT * FROM parses
        WHERE file_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (file_id,),
    )


def get_chunk_artifacts(cur, chunk_id: int) -> Optional[dict]:
    return fetch_one(
        cur,
        """
        SELECT
            c.id AS chunk_id,
            c.page AS chunk_page,
            c.chunk_index,
            c.text AS chunk_text,
            e.id AS embedding_id,
            e.content_hash AS embedding_content_hash,
            p.id AS parse_id,
            p.file_id,
            p.reducto_file_id,
            p.parse_job_id,
            p.extract_job_id,
            p.parse_json,
            p.extract_json,
            p.parsed_text,
            p.created_at AS parse_created_at,
            f.file_hash,
            f.filename,
            f.mime_type,
            f.size_bytes,
            f.local_path,
            a.id AS archive_id,
            a.r2_bucket,
            a.r2_key,
            a.archived_at
        FROM chunks c
        JOIN embeddings e ON e.id = c.embedding_id
        LEFT JOIN LATERAL (
            SELECT *
            FROM parses p
            WHERE p.content_hash = e.content_hash
            ORDER BY p.created_at DESC
            LIMIT 1
        ) p ON TRUE
        LEFT JOIN files f ON f.id = p.file_id
        LEFT JOIN LATERAL (
            SELECT *
            FROM archives a
            WHERE a.file_id = f.id
            ORDER BY a.archived_at DESC
            LIMIT 1
        ) a ON TRUE
        WHERE c.id = %s
        LIMIT 1
        """,
        (chunk_id,),
    )


def create_parse(
    cur,
    file_id: int,
    reducto_file_id: Optional[str],
    parse_job_id: Optional[str],
    extract_job_id: Optional[str],
    parse_json: Optional[dict],
    extract_json: Optional[dict],
    parsed_text: Optional[str],
    content_hash: Optional[str],
) -> dict:
    import json
    cur.execute(
        """
        INSERT INTO parses (
            file_id, reducto_file_id, parse_job_id, extract_job_id,
            parse_json, extract_json, parsed_text, content_hash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            file_id,
            reducto_file_id,
            parse_job_id,
            extract_job_id,
            json.dumps(parse_json) if parse_json else None,
            json.dumps(extract_json) if extract_json else None,
            parsed_text,
            content_hash,
        ),
    )
    row = cur.fetchone()
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))


def get_embedding_by_fingerprint(cur, fingerprint: str) -> Optional[dict]:
    return fetch_one(
        cur,
        """
        SELECT * FROM embeddings
        WHERE fingerprint = %s
        """,
        (fingerprint,),
    )


def get_embedding_by_content_hash(cur, content_hash: str) -> Optional[dict]:
    return fetch_one(
        cur,
        """
        SELECT * FROM embeddings
        WHERE content_hash = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (content_hash,),
    )


def create_embedding(cur, content_hash: str, model: str, dims: int, fingerprint: str) -> dict:
    cur.execute(
        """
        INSERT INTO embeddings (content_hash, model, dims, fingerprint)
        VALUES (%s, %s, %s, %s)
        RETURNING *
        """,
        (content_hash, model, dims, fingerprint),
    )
    row = cur.fetchone()
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))


def insert_chunks(cur, embedding_id: int, chunks: list[dict]) -> None:
    if not chunks:
        return
    rows = [
        (
            embedding_id,
            chunk.get("page"),
            chunk.get("chunk_index"),
            chunk.get("text"),
            len(chunk.get("text", "").split()),
            chunk.get("embedding"),
        )
        for chunk in chunks
    ]
    from psycopg2.extras import execute_values

    execute_values(
        cur,
        """
        INSERT INTO chunks (embedding_id, page, chunk_index, text, token_count, embedding)
        VALUES %s
        """,
        rows,
        template="(%s, %s, %s, %s, %s, %s::vector)",
    )


def get_archive_by_key(cur, bucket: str, key: str) -> Optional[dict]:
    return fetch_one(
        cur,
        """
        SELECT * FROM archives
        WHERE r2_bucket = %s AND r2_key = %s
        """,
        (bucket, key),
    )


def create_archive(
    cur,
    file_id: int,
    bucket: str,
    key: str,
    etag: Optional[str],
    size_bytes: Optional[int],
    company_slug: Optional[str],
) -> dict:
    cur.execute(
        """
        INSERT INTO archives (file_id, r2_bucket, r2_key, etag, size_bytes, company_slug)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (file_id, bucket, key, etag, size_bytes, company_slug),
    )
    row = cur.fetchone()
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))


def find_source_by_url(cur, url: str) -> Optional[dict]:
    return fetch_one(
        cur,
        """
        SELECT * FROM sources
        WHERE canonical_url = %s OR raw_url = %s
        """,
        (url, url),
    )


def update_source_metadata(cur, source_id: int, metadata: dict) -> None:
    import json
    cur.execute(
        """
        UPDATE sources
        SET metadata = metadata || %s, updated_at = NOW()
        WHERE id = %s
        """,
        (json.dumps(metadata), source_id),
    )


def get_file_source_metadata(cur, file_id: int) -> Optional[dict]:
    return fetch_one(
        cur,
        """
        SELECT s.metadata AS source_metadata, fs.metadata AS file_metadata
        FROM file_sources fs
        JOIN sources s ON fs.source_id = s.id
        WHERE fs.file_id = %s
        ORDER BY fs.created_at DESC
        LIMIT 1
        """,
        (file_id,),
    )


def find_file_by_source(cur, source_id: int) -> list[dict]:
    cur.execute(
        """
        SELECT files.*
        FROM files
        JOIN file_sources ON file_sources.file_id = files.id
        WHERE file_sources.source_id = %s
        """,
        (source_id,),
    )
    rows = cur.fetchall()
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in rows]
