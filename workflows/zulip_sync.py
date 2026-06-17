"""Workflow: sync recent public Zulip stream history into Postgres."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from typing import Any

from api.runtime_control import canonical_json
from api.vm_metrics import (
    record_etl_items_enqueued,
    record_etl_items_failed,
    record_etl_items_seen,
    record_etl_items_upserted,
)
from api.workflow_engine import WorkflowContext
from workflows.zulip_sync_shared import (
    client as shared_client,
    env_flag_enabled,
    failure_reason,
    load_stream_checkpoints,
    message_row,
    positive_int,
    record_run_finish,
    record_run_start,
    seed_stream_bootstrap_job,
    stream_ref,
    update_checkpoint,
    upsert_messages,
    workflow_run_id_to_sync_run_id,
)

WORKFLOW_NAME = "zulip_sync"

DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_STREAM_PAGE_LIMIT = 200
DEFAULT_SYNC_INTERVAL_SECONDS = 3_600
EXCLUDED_STREAMS_ENV = "ZULIP_ETL_EXCLUDED_STREAM_PATTERNS"


def _env_flag_enabled(name: str, default: bool = False) -> bool:
    """Read a boolean feature flag where common false strings opt out."""
    return env_flag_enabled(name, default=default)


SCHEDULE = {
    "schedule_id": "zulip_sync",
    "interval_seconds": positive_int(
        os.getenv("ZULIP_SYNC_INTERVAL_SECONDS"),
        DEFAULT_SYNC_INTERVAL_SECONDS,
    ),
    "enabled": _env_flag_enabled("ZULIP_ETL_ENABLED"),
    "no_delivery": True,
}


@dataclass
class Input:
    """Runtime options for a manual Zulip sync workflow run."""

    lookback_days: int | None = None
    limit: int = DEFAULT_STREAM_PAGE_LIMIT
    metadata: dict[str, Any] = field(default_factory=dict)


def _stream_exclusion_patterns(value: str | None) -> list[str]:
    """Parse comma-separated Zulip stream exclusion globs."""
    if not value:
        return []
    patterns = []
    for raw_pattern in value.split(","):
        pattern = raw_pattern.strip().lower().lstrip("#")
        if pattern:
            patterns.append(pattern)
    return patterns


def _stream_name(stream: dict[str, Any]) -> str:
    """Return the normalized stream name used for config matching."""
    return str(stream.get("name") or "").strip().lower().lstrip("#")


def _stream_exclusion_reason(
    stream: dict[str, Any],
    patterns: list[str],
) -> str | None:
    """Return the configured pattern excluding a stream, if any."""
    name = _stream_name(stream)
    if not name:
        return None
    for pattern in patterns:
        if fnmatch.fnmatchcase(name, pattern):
            return f"excluded_by_config:{pattern}"
    return None


def _is_public_syncable_stream(stream: dict[str, Any]) -> bool:
    """Return whether the stream should be considered public history."""
    if bool(stream.get("is_archived")):
        return False
    return not bool(stream.get("invite_only"))


def _filter_streams(
    streams: list[dict[str, Any]],
    patterns: list[str],
    *,
    realm: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Split Zulip streams into included streams and configured exclusions."""
    included = []
    skipped = []
    for stream in streams:
        if not _is_public_syncable_stream(stream):
            skipped.append(stream_ref(stream, realm=realm, reason="not_public_syncable"))
            continue
        reason = _stream_exclusion_reason(stream, patterns)
        if reason:
            skipped.append(stream_ref(stream, realm=realm, reason=reason))
        else:
            included.append(stream)
    return included, skipped


async def _upsert_streams(
    pool,
    *,
    realm: str,
    streams: list[dict[str, Any]],
    syncable_stream_ids: set[int],
) -> None:
    """Refresh Zulip stream catalog rows and mark absent streams out of scope."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE zulip_sync_streams SET is_syncable = FALSE, updated_at = NOW() "
                "WHERE realm = $1",
                realm,
            )
            for stream in streams:
                stream_id = int(stream.get("stream_id") or 0)
                if not stream_id:
                    continue
                await conn.execute(
                    "INSERT INTO zulip_sync_streams ("
                    "realm, stream_id, stream_name, description, is_archived, is_public, "
                    "is_web_public, is_syncable, raw_payload, last_seen_at, updated_at"
                    ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW(), NOW()) "
                    "ON CONFLICT (realm, stream_id) DO UPDATE SET "
                    "stream_name = EXCLUDED.stream_name, "
                    "description = EXCLUDED.description, "
                    "is_archived = EXCLUDED.is_archived, "
                    "is_public = EXCLUDED.is_public, "
                    "is_web_public = EXCLUDED.is_web_public, "
                    "is_syncable = EXCLUDED.is_syncable, "
                    "raw_payload = EXCLUDED.raw_payload, "
                    "last_seen_at = NOW(), "
                    "updated_at = NOW()",
                    realm,
                    stream_id,
                    str(stream.get("name") or ""),
                    str(stream.get("description") or ""),
                    bool(stream.get("is_archived")),
                    not bool(stream.get("invite_only")),
                    bool(stream.get("is_web_public")),
                    stream_id in syncable_stream_ids,
                    canonical_json(stream),
                )


async def _upsert_users(pool, *, realm: str, users: list[dict[str, Any]]) -> int:
    """Refresh Zulip user directory rows."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            for user in users:
                user_id = int(user.get("user_id") or user.get("id") or 0)
                if not user_id:
                    continue
                await conn.execute(
                    "INSERT INTO zulip_sync_users ("
                    "realm, user_id, email, full_name, is_bot, is_active, raw_payload, "
                    "last_seen_at, updated_at"
                    ") VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW(), NOW()) "
                    "ON CONFLICT (realm, user_id) DO UPDATE SET "
                    "email = EXCLUDED.email, "
                    "full_name = EXCLUDED.full_name, "
                    "is_bot = EXCLUDED.is_bot, "
                    "is_active = EXCLUDED.is_active, "
                    "raw_payload = EXCLUDED.raw_payload, "
                    "last_seen_at = NOW(), "
                    "updated_at = NOW()",
                    realm,
                    user_id,
                    str(user.get("email") or ""),
                    str(user.get("full_name") or ""),
                    bool(user.get("is_bot")),
                    not bool(user.get("is_deactivated")),
                    canonical_json(user),
                )
    return len(users)


async def _upsert_topics(
    pool,
    *,
    realm: str,
    stream_id: int,
    topics: list[dict[str, Any]],
) -> int:
    """Refresh Zulip topic rows for one stream."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            for topic in topics:
                topic_name = str(topic.get("name") or "").strip()
                if not topic_name:
                    continue
                await conn.execute(
                    "INSERT INTO zulip_sync_topics ("
                    "realm, stream_id, topic_name, max_id, message_count, raw_payload, "
                    "last_seen_at, updated_at"
                    ") VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW(), NOW()) "
                    "ON CONFLICT (realm, stream_id, topic_name) DO UPDATE SET "
                    "max_id = EXCLUDED.max_id, "
                    "message_count = EXCLUDED.message_count, "
                    "raw_payload = EXCLUDED.raw_payload, "
                    "last_seen_at = NOW(), "
                    "updated_at = NOW()",
                    realm,
                    stream_id,
                    topic_name,
                    int(topic["max_id"]) if topic.get("max_id") is not None else None,
                    int(topic.get("message_count") or 0),
                    canonical_json(topic),
                )
    return len(topics)


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    """Sync recent Zulip public stream history into Postgres."""
    if not env_flag_enabled("ZULIP_ETL_ENABLED", default=False):
        ctx.log("zulip_sync_skipped_disabled")
        return {
            "status": "skipped",
            "reason": "zulip_etl_disabled",
        }

    limit = positive_int(inp.limit, DEFAULT_STREAM_PAGE_LIMIT)
    lookback_days = positive_int(
        inp.lookback_days or os.getenv("ZULIP_SYNC_BACKFILL_LOOKBACK_DAYS"),
        DEFAULT_LOOKBACK_DAYS,
    )
    client = shared_client()
    realm = client.realm
    access_mode = client._etl_access_mode()
    run_id = workflow_run_id_to_sync_run_id(ctx.run_id)

    streams = client._list_etl_streams()
    exclusion_patterns = _stream_exclusion_patterns(os.getenv(EXCLUDED_STREAMS_ENV))
    syncable_streams, skipped = _filter_streams(
        streams,
        exclusion_patterns,
        realm=realm,
    )
    syncable_stream_ids = {
        int(stream.get("stream_id") or 0)
        for stream in syncable_streams
        if int(stream.get("stream_id") or 0)
    }
    await _upsert_streams(
        ctx._pool,
        realm=realm,
        streams=streams,
        syncable_stream_ids=syncable_stream_ids,
    )

    users = client._list_etl_users()
    users_upserted = await _upsert_users(ctx._pool, realm=realm, users=users)
    record_etl_items_upserted("zulip", "user", "row", users_upserted)

    requested = [stream_ref(stream, realm=realm) for stream in syncable_streams]
    await record_run_start(
        ctx._pool,
        run_id=run_id,
        workflow_run_id=ctx.run_id,
        mode="incremental",
        realm=realm,
        requested=requested,
        skipped=skipped,
        metadata={
            **inp.metadata,
            "zulip_access_mode": access_mode,
            "lookback_days": lookback_days,
            "limit": limit,
            "excluded_stream_patterns": exclusion_patterns,
        },
    )

    checkpoints = await load_stream_checkpoints(
        ctx._pool,
        realm=realm,
        stream_ids=list(syncable_stream_ids),
    )
    synced: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    counts = {"topics_fetched": 0, "messages_fetched": 0, "messages_upserted": 0}

    for stream in syncable_streams:
        stream_id = int(stream.get("stream_id") or 0)
        if not stream_id:
            continue
        try:
            topics = client._list_stream_topics(stream_id)
            counts["topics_fetched"] += len(topics)
            await _upsert_topics(
                ctx._pool,
                realm=realm,
                stream_id=stream_id,
                topics=topics,
            )
            state = {}
            if checkpoints.get(stream_id):
                state["anchor"] = checkpoints[stream_id]
            page = client._sync_etl_stream_history(
                stream_id=stream_id,
                state=state,
                limit=limit,
                lookback_days=lookback_days,
            )
            rows = [
                message_row(message, run_id, realm=realm, stream_id=stream_id)
                for message in page.get("messages", [])
            ]
            counts["messages_fetched"] += len(rows)
            record_etl_items_seen("zulip", "stream", "message", len(rows))
            upserted = await upsert_messages(ctx._pool, rows)
            counts["messages_upserted"] += upserted
            record_etl_items_upserted("zulip", "stream", "message", upserted)
            watermark = max(
                (int(row["message_id"]) for row in rows if row.get("message_id")),
                default=checkpoints.get(stream_id),
            )
            await update_checkpoint(
                ctx._pool,
                realm=realm,
                stream_id=stream_id,
                watermark_message_id=watermark,
                run_id=run_id,
            )
            await seed_stream_bootstrap_job(
                ctx._pool,
                realm=realm,
                stream_id=stream_id,
                lookback_days=lookback_days,
            )
            record_etl_items_enqueued("zulip", "stream", "backfill_job", 1)
            synced.append(stream_ref(stream, realm=realm))
        except Exception as exc:
            error_text = str(exc)
            failed_ref = stream_ref(stream, realm=realm, reason=error_text[:200])
            failed.append(failed_ref)
            record_etl_items_failed(
                "zulip",
                "stream",
                "message",
                failure_reason(error_text),
            )
            await update_checkpoint(
                ctx._pool,
                realm=realm,
                stream_id=stream_id,
                watermark_message_id=None,
                run_id=run_id,
                error=error_text[:1000],
            )
            ctx.log(
                "zulip_sync_stream_failed",
                stream_id=stream_id,
                stream_name=stream.get("name"),
                error=error_text,
            )

    status = "success" if not failed else "partial_failure"
    await record_run_finish(
        ctx._pool,
        run_id=run_id,
        status=status,
        synced=synced,
        skipped=skipped,
        failed=failed,
        counts=counts,
        error_text="" if not failed else "one or more Zulip streams failed",
    )

    return {
        "status": status,
        "realm": realm,
        "streams_requested": len(requested),
        "streams_synced": len(synced),
        "streams_skipped": len(skipped),
        "streams_failed": len(failed),
        **counts,
    }
