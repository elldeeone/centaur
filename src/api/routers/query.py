from __future__ import annotations

from datetime import date
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_pool, verify_api_key

router = APIRouter(prefix="/api/query", dependencies=[Depends(verify_api_key)])


@router.get("/slack/messages")
async def slack_messages(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    channel: str | None = None,
    user: str | None = None,
    after: date | None = None,
    before: date | None = None,
    text: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Query Slack messages directly from raw_records JSONB."""
    conditions = ["source = 'slack'", "kind = 'message'"]
    args: list = []
    idx = 1

    if channel:
        conditions.append(f"data->>'channel' = ${idx}")
        args.append(channel)
        idx += 1
    if user:
        conditions.append(f"data->>'user' = ${idx}")
        args.append(user)
        idx += 1
    if after:
        conditions.append(f"fetched_at >= ${idx}")
        args.append(after)
        idx += 1
    if before:
        conditions.append(f"fetched_at <= ${idx}")
        args.append(before)
        idx += 1
    if text:
        conditions.append(f"data->>'text' ILIKE ${idx}")
        args.append(f"%{text}%")
        idx += 1

    where = "WHERE " + " AND ".join(conditions)
    args.extend([limit, offset])

    sql = f"""
    SELECT DISTINCT ON (source, kind, external_id)
        external_id,
        data->>'channel' AS channel_id,
        data->>'user' AS user_id,
        data->>'text' AS text,
        data->>'ts' AS slack_ts,
        data->>'thread_ts' AS thread_ts,
        (data->>'reply_count')::int AS reply_count,
        fetched_at
    FROM raw_records
    {where}
    ORDER BY source, kind, external_id, fetched_at DESC
    LIMIT ${idx} OFFSET ${idx + 1}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


@router.get("/slack/threads")
async def slack_threads(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    channel: str | None = None,
    after: date | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    """Aggregate Slack threads from raw_records."""
    conditions = [
        "source = 'slack'",
        "kind = 'message'",
        "data->>'thread_ts' IS NOT NULL",
        "data->>'thread_ts' != ''",
    ]
    args: list = []
    idx = 1

    if channel:
        conditions.append(f"data->>'channel' = ${idx}")
        args.append(channel)
        idx += 1
    if after:
        conditions.append(f"fetched_at >= ${idx}")
        args.append(after)
        idx += 1

    where = "WHERE " + " AND ".join(conditions)
    args.append(limit)

    sql = f"""
    WITH latest AS (
        SELECT DISTINCT ON (source, kind, external_id)
            data->>'channel' AS channel_id,
            data->>'thread_ts' AS thread_ts,
            data->>'ts' AS slack_ts,
            data->>'user' AS user_id,
            data->>'text' AS text
        FROM raw_records
        {where}
        ORDER BY source, kind, external_id, fetched_at DESC
    )
    SELECT
        channel_id,
        thread_ts,
        COUNT(*) - 1 AS reply_count,
        COUNT(DISTINCT user_id) AS participant_count,
        MIN(slack_ts) AS started_at,
        MAX(slack_ts) AS last_reply_at,
        string_agg(text, E'\n' ORDER BY slack_ts) AS all_text
    FROM latest
    GROUP BY channel_id, thread_ts
    HAVING COUNT(*) > 1
    ORDER BY MAX(slack_ts) DESC
    LIMIT ${idx}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


@router.get("/linear/issues")
async def linear_issues(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    state: str | None = None,
    assignee: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    """Query Linear issues from raw_records JSONB."""
    conditions = ["source = 'linear'", "kind = 'issue'"]
    args: list = []
    idx = 1

    if state:
        conditions.append(f"data->'state'->>'name' ILIKE ${idx}")
        args.append(f"%{state}%")
        idx += 1
    if assignee:
        conditions.append(f"data->'assignee'->>'name' ILIKE ${idx}")
        args.append(f"%{assignee}%")
        idx += 1

    where = "WHERE " + " AND ".join(conditions)
    args.append(limit)

    sql = f"""
    SELECT DISTINCT ON (source, kind, external_id)
        external_id AS id,
        data->>'identifier' AS identifier,
        data->>'title' AS title,
        data->'state'->>'name' AS state,
        data->'assignee'->>'name' AS assignee,
        data->>'priorityLabel' AS priority,
        data->>'url' AS url,
        (data->>'createdAt')::timestamptz AS created_at,
        (data->>'updatedAt')::timestamptz AS updated_at
    FROM raw_records
    {where}
    ORDER BY source, kind, external_id, fetched_at DESC
    LIMIT ${idx}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


@router.get("/github/prs")
async def github_prs(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    repo: str | None = None,
    author: str | None = None,
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    """Query GitHub PRs from raw_records JSONB."""
    conditions = ["source = 'github'", "kind = 'pull_request'"]
    args: list = []
    idx = 1

    if repo:
        conditions.append(f"data->'base'->'repo'->>'full_name' ILIKE ${idx}")
        args.append(f"%{repo}%")
        idx += 1
    if author:
        conditions.append(f"data->'user'->>'login' = ${idx}")
        args.append(author)
        idx += 1
    if state:
        conditions.append(f"data->>'state' = ${idx}")
        args.append(state)
        idx += 1

    where = "WHERE " + " AND ".join(conditions)
    args.append(limit)

    sql = f"""
    SELECT DISTINCT ON (source, kind, external_id)
        external_id AS id,
        data->>'title' AS title,
        data->>'state' AS state,
        data->'user'->>'login' AS author,
        data->'base'->'repo'->>'full_name' AS repo,
        data->>'html_url' AS url,
        (data->>'created_at')::timestamptz AS created_at,
        (data->>'merged_at')::timestamptz AS merged_at
    FROM raw_records
    {where}
    ORDER BY source, kind, external_id, fetched_at DESC
    LIMIT ${idx}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


@router.get("/timeline")
async def timeline(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    days: int = Query(default=7, ge=1, le=90),
    source: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Unified activity timeline from raw_records."""
    conditions = ["fetched_at >= NOW() - make_interval(days => $1)"]
    args: list = [days]
    idx = 2

    if source:
        conditions.append(f"source = ${idx}")
        args.append(source)
        idx += 1

    where = "WHERE " + " AND ".join(conditions)
    args.append(limit)

    sql = f"""
    SELECT DISTINCT ON (source, kind, external_id)
        source,
        kind,
        external_id,
        COALESCE(data->>'title', data->>'text', data->>'name', data->>'summary') AS title,
        fetched_at,
        COALESCE(data->>'url', data->>'html_url', data->>'htmlLink') AS url
    FROM raw_records
    {where}
    ORDER BY source, kind, external_id, fetched_at DESC
    LIMIT ${idx}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


@router.get("/people")
async def list_people(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.slug, p.name, p.email, p.role, p.is_direct_report, p.focus_area,
                   json_agg(json_build_object('source', em.source, 'id', em.external_id))
                       FILTER (WHERE em.source IS NOT NULL) AS identities
            FROM people p
            LEFT JOIN entity_mappings em ON em.person_slug = p.slug
            GROUP BY p.slug, p.name, p.email, p.role, p.is_direct_report, p.focus_area
            ORDER BY p.name
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@router.get("/people/{slug}")
async def get_person(
    slug: str,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    async with pool.acquire() as conn:
        person = await conn.fetchrow(
            """
            SELECT p.slug, p.name, p.email, p.role, p.focus_area,
                   json_agg(json_build_object('source', em.source, 'id', em.external_id))
                       FILTER (WHERE em.source IS NOT NULL) AS identities
            FROM people p
            LEFT JOIN entity_mappings em ON em.person_slug = p.slug
            WHERE p.slug = $1
            GROUP BY p.slug, p.name, p.email, p.role, p.focus_area
            """,
            slug,
        )
        if not person:
            raise HTTPException(status_code=404, detail="Person not found")

    return dict(person)
