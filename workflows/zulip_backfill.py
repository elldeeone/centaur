"""Workflow: drain resumable Zulip ETL backfill cursors."""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Any

from api.vm_metrics import (
    record_etl_items_enqueued,
    record_etl_items_failed,
    record_etl_items_seen,
    record_etl_items_upserted,
)
from api.workflow_engine import WorkflowContext
from workflows.zulip_sync_shared import (
    BACKFILL_JOB_PAYLOAD_VERSION,
    BACKFILL_JOB_STREAM_BOOTSTRAP,
    BACKFILL_JOB_STREAM_CONTINUATION,
    claim_backfill_jobs,
    client as shared_client,
    enqueue_backfill_job,
    env_flag_enabled,
    failure_reason,
    mark_backfill_job_completed,
    mark_backfill_job_failed,
    message_row,
    positive_int,
    record_run_finish,
    record_run_start,
    upsert_messages,
    workflow_run_id_to_sync_run_id,
    zulip_ts_to_datetime,
)

WORKFLOW_NAME = "zulip_backfill"

DEFAULT_STREAM_PAGE_LIMIT = 200
DEFAULT_SYNC_INTERVAL_SECONDS = 10 * 60
DEFAULT_STREAM_BATCH_LIMIT = positive_int(
    os.getenv("ZULIP_BACKFILL_STREAM_BATCH_LIMIT"),
    50,
)
DEFAULT_STREAM_PAGES_PER_JOB = positive_int(
    os.getenv("ZULIP_BACKFILL_STREAM_PAGES_PER_JOB"),
    5,
)
DEFAULT_LOOKBACK_DAYS = 30

SCHEDULE = {
    "schedule_id": "zulip_backfill",
    "interval_seconds": positive_int(
        os.getenv("ZULIP_BACKFILL_INTERVAL_SECONDS"),
        DEFAULT_SYNC_INTERVAL_SECONDS,
    ),
    "enabled": (
        env_flag_enabled("ZULIP_ETL_ENABLED", default=False)
        and env_flag_enabled("ZULIP_BACKFILL_ENABLED", default=True)
    ),
    "no_delivery": True,
}


@dataclass
class Input:
    """Runtime options for Zulip historical backfill draining."""

    limit: int = DEFAULT_STREAM_PAGE_LIMIT
    stream_batch_limit: int = DEFAULT_STREAM_BATCH_LIMIT
    stream_pages_per_job: int = DEFAULT_STREAM_PAGES_PER_JOB
    metadata: dict[str, Any] = field(default_factory=dict)


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    """Validate and extract a typed stream-history backfill payload."""
    if str(job.get("job_type") or "") not in {
        BACKFILL_JOB_STREAM_BOOTSTRAP,
        BACKFILL_JOB_STREAM_CONTINUATION,
    }:
        raise RuntimeError(f"unsupported backfill job type: {job.get('job_type')}")
    if int(job.get("payload_version") or 0) != BACKFILL_JOB_PAYLOAD_VERSION:
        raise RuntimeError(
            f"unsupported payload version for {job.get('job_key')}: {job.get('payload_version')}"
        )
    payload = job.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid payload for {job.get('job_key')}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid payload for {job.get('job_key')}")
    return payload


def _continuation_job_key(realm: str, stream_id: int, anchor: int) -> str:
    """Return the stable job key for a bounded older-history continuation."""
    return f"continuation:{realm}:{stream_id}:{anchor}"


def _within_lookback(message: dict[str, Any], *, cutoff: dt.datetime) -> bool:
    """Return whether a Zulip message falls within the configured backfill window."""
    occurred_at = zulip_ts_to_datetime(message.get("timestamp"))
    if occurred_at is None:
        return True
    return occurred_at >= cutoff


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    """Drain queued Zulip backfill continuations in small, bounded batches."""
    if not (
        env_flag_enabled("ZULIP_ETL_ENABLED", default=False)
        and env_flag_enabled("ZULIP_BACKFILL_ENABLED", default=True)
    ):
        ctx.log("zulip_backfill_skipped_disabled")
        return {
            "status": "skipped",
            "reason": "zulip_backfill_disabled",
        }

    limit = positive_int(inp.limit, DEFAULT_STREAM_PAGE_LIMIT)
    stream_batch_limit = positive_int(
        inp.stream_batch_limit,
        DEFAULT_STREAM_BATCH_LIMIT,
    )
    stream_pages_per_job = positive_int(
        inp.stream_pages_per_job,
        DEFAULT_STREAM_PAGES_PER_JOB,
    )
    jobs = await claim_backfill_jobs(ctx._pool, stream_batch_limit)
    if not jobs:
        ctx.log("zulip_backfill_skipped_no_jobs")
        return {
            "status": "skipped",
            "reason": "no_pending_backfills",
        }

    client = shared_client()
    access_mode = client._etl_access_mode()
    run_id = workflow_run_id_to_sync_run_id(ctx.run_id)
    realm = client.realm
    requested = [
        {
            "realm": str(job["realm"]),
            "stream_id": str(job["stream_id"]),
            "stream_name": "",
            "reason": str(job["job_key"]),
        }
        for job in jobs
    ]
    await record_run_start(
        ctx._pool,
        run_id=run_id,
        workflow_run_id=ctx.run_id,
        mode="backfill",
        realm=realm,
        requested=requested,
        skipped=[],
        metadata={
            **inp.metadata,
            "zulip_access_mode": access_mode,
            "backfill_stream_batch_limit": stream_batch_limit,
            "backfill_stream_pages_per_job": stream_pages_per_job,
        },
    )

    synced: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    counts = {"topics_fetched": 0, "messages_fetched": 0, "messages_upserted": 0}

    for job in jobs:
        job_id = int(job["job_id"])
        job_realm = str(job["realm"] or realm)
        stream_id = int(job["stream_id"])
        try:
            payload = _job_payload(job)
            anchor: str | int = payload.get("anchor") or "newest"
            lookback_days = positive_int(
                payload.get("lookback_days"),
                DEFAULT_LOOKBACK_DAYS,
            )
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
            topic_name = str(job.get("topic_name") or "") or None
            should_continue = False

            for _ in range(stream_pages_per_job):
                page = client._backfill_stream_history(
                    stream_id=stream_id,
                    anchor=anchor,
                    limit=limit,
                    topic_name=topic_name,
                )
                messages = [
                    message
                    for message in page.get("messages", [])
                    if _within_lookback(message, cutoff=cutoff)
                ]
                rows = [
                    message_row(message, run_id, realm=job_realm, stream_id=stream_id)
                    for message in messages
                ]
                counts["messages_fetched"] += len(rows)
                record_etl_items_seen("zulip", "stream", "message", len(rows))
                upserted = await upsert_messages(ctx._pool, rows)
                counts["messages_upserted"] += upserted
                record_etl_items_upserted("zulip", "stream", "message", upserted)

                next_anchor = page.get("next_anchor")
                found_oldest = bool(page.get("found_oldest"))
                hit_cutoff = len(messages) < len(page.get("messages", []))
                if found_oldest or hit_cutoff or not next_anchor:
                    should_continue = False
                    break
                anchor = int(next_anchor)
                should_continue = True

            if should_continue:
                await enqueue_backfill_job(
                    ctx._pool,
                    job_key=_continuation_job_key(job_realm, stream_id, int(anchor)),
                    job_type=BACKFILL_JOB_STREAM_CONTINUATION,
                    realm=job_realm,
                    stream_id=stream_id,
                    topic_name=topic_name or "",
                    payload={"anchor": anchor, "lookback_days": lookback_days},
                    priority=int(job.get("priority") or 100) + 10,
                )
                record_etl_items_enqueued("zulip", "stream", "backfill_job", 1)

            await mark_backfill_job_completed(ctx._pool, job_id)
            synced.append(
                {
                    "realm": job_realm,
                    "stream_id": str(stream_id),
                    "stream_name": "",
                    "reason": str(job.get("job_key") or ""),
                }
            )
        except Exception as exc:
            error_text = str(exc)
            failed.append(
                {
                    "realm": job_realm,
                    "stream_id": str(stream_id),
                    "stream_name": "",
                    "reason": error_text[:200],
                }
            )
            record_etl_items_failed(
                "zulip",
                "stream",
                "message",
                failure_reason(error_text),
            )
            await mark_backfill_job_failed(ctx._pool, job_id, error_text[:1000])
            ctx.log(
                "zulip_backfill_stream_failed",
                stream_id=stream_id,
                error=error_text,
            )

    status = "success" if not failed else "partial_failure"
    await record_run_finish(
        ctx._pool,
        run_id=run_id,
        status=status,
        synced=synced,
        skipped=[],
        failed=failed,
        counts=counts,
        error_text="" if not failed else "one or more Zulip backfill jobs failed",
    )

    return {
        "status": status,
        "jobs_claimed": len(jobs),
        "jobs_completed": len(synced),
        "jobs_failed": len(failed),
        **counts,
    }
