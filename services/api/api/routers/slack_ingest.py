"""Slack event ingestion routes for event-driven ETL updates."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.api_keys import check_scope
from api.deps import get_key_info, verify_api_key
from workflows.company_context_documents import Input as CompanyContextInput
from workflows.company_context_documents import handler as project_company_context
from workflows.slack_sync_shared import message_row, upsert_messages

log = structlog.get_logger().bind(service="api", component="slack_ingest")

router = APIRouter(
    prefix="/api/slack",
    tags=["slack"],
    dependencies=[Depends(verify_api_key)],
)

FALSE_ENV_VALUES = {"0", "false", "no", "off"}


class SlackEventIngestRequest(BaseModel):
    envelope: dict[str, Any] = Field(default_factory=dict)
    project_context: bool = True


@dataclass(frozen=True)
class _IngestedEvent:
    channel_id: str
    message_ts: str
    action: str


class _InlineWorkflowContext:
    def __init__(self, pool, run_id: str) -> None:
        self._pool = pool
        self.run_id = run_id

    def log(self, msg: str, **kwargs: Any) -> None:
        log.info(msg, **kwargs)


def _env_flag_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in FALSE_ENV_VALUES


def _require_slack_ingest_access(request: Request) -> None:
    info = get_key_info(request)
    if (
        check_scope(info, "admin")
        or check_scope(info, "agent")
        or check_scope(info, "slack:ingest")
    ):
        return
    raise HTTPException(
        status_code=403,
        detail="API key scope does not permit Slack event ingestion.",
    )


def _message_event(envelope: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    if envelope.get("type") != "event_callback":
        return None
    event = envelope.get("event")
    if not isinstance(event, dict):
        return None
    if event.get("type") not in {"message", "app_mention"}:
        return None

    subtype = str(event.get("subtype") or "")
    if subtype == "message_changed":
        changed = event.get("message")
        if not isinstance(changed, dict):
            return None
        return {**changed, "channel": changed.get("channel") or event.get("channel")}, "upsert"

    if subtype == "message_deleted":
        previous = event.get("previous_message")
        deleted_ts = event.get("deleted_ts")
        if isinstance(previous, dict):
            message = {
                **previous,
                "ts": previous.get("ts") or deleted_ts,
                "channel": previous.get("channel") or event.get("channel"),
                "subtype": "message_deleted",
                "text": "",
            }
        else:
            message = {
                "type": "message",
                "subtype": "message_deleted",
                "channel": event.get("channel"),
                "ts": deleted_ts,
                "text": "",
            }
        return message, "upsert"

    return event, "upsert"


def _event_message_row(
    *,
    envelope: dict[str, Any],
    message: dict[str, Any],
    source_run_id: str | None = None,
) -> dict[str, Any] | None:
    channel_id = str(message.get("channel") or "").strip()
    message_ts = str(message.get("ts") or "").strip()
    if not channel_id or not message_ts:
        return None

    projected = {
        "channel_id": channel_id,
        "timestamp": message_ts,
        "thread_ts": message.get("thread_ts"),
        "user_id": message.get("user"),
        "bot_id": message.get("bot_id"),
        "type": message.get("type") or "message",
        "subtype": message.get("subtype"),
        "text": message.get("text") or "",
        "reply_count": message.get("reply_count") or 0,
        "reply_users": message.get("reply_users") or [],
        "latest_reply": message.get("latest_reply"),
        "event_id": envelope.get("event_id"),
        "event_ts": message.get("event_ts"),
        "team_id": envelope.get("team_id") or message.get("team"),
        "enterprise_id": envelope.get("enterprise_id"),
        "raw_event": envelope.get("event"),
        "raw_message": message,
    }
    thread_ts = projected.get("thread_ts")
    parent_message_ts = (
        str(thread_ts)
        if isinstance(thread_ts, str) and thread_ts.strip() and thread_ts != message_ts
        else None
    )
    return message_row(projected, source_run_id, parent_message_ts)


async def _ensure_channel_row(pool, channel_id: str) -> None:
    await pool.execute(
        "INSERT INTO slack_sync_channels ("
        "channel_id, channel_name, is_syncable, last_seen_at, updated_at"
        ") VALUES ($1, '', TRUE, NOW(), NOW()) "
        "ON CONFLICT (channel_id) DO UPDATE SET "
        "last_seen_at = NOW(), "
        "updated_at = NOW()",
        channel_id,
    )


async def _project_context_inline(pool, *, channel_id: str, message_ts: str) -> bool:
    if not (
        _env_flag_enabled("SLACK_EVENT_INGEST_PROJECT_INLINE", default=True)
        and _env_flag_enabled("COMPANY_CONTEXT_DOCUMENTS_ENABLED", default=True)
    ):
        return False
    occurred_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)
    ctx = _InlineWorkflowContext(
        pool,
        run_id=f"inline-slack-event-{channel_id}-{message_ts}".replace(".", "_"),
    )
    await project_company_context(
        CompanyContextInput(
            since=occurred_at.isoformat(),
            watermark_overlap_seconds=0,
            metadata={"trigger": "slack_event_ingest"},
        ),
        ctx,
    )
    return True


async def _ingest_slack_event(pool, envelope: dict[str, Any]) -> _IngestedEvent | None:
    parsed = _message_event(envelope)
    if parsed is None:
        return None
    message, action = parsed
    row = _event_message_row(envelope=envelope, message=message)
    if row is None:
        return None
    await _ensure_channel_row(pool, row["channel_id"])
    await upsert_messages(pool, [row])
    return _IngestedEvent(
        channel_id=row["channel_id"],
        message_ts=row["message_ts"],
        action=action,
    )


@router.post("/events/ingest")
async def ingest_slack_event(request: Request, body: SlackEventIngestRequest):
    _require_slack_ingest_access(request)
    if not _env_flag_enabled("SLACK_ETL_ENABLED"):
        return {"ok": True, "status": "skipped", "reason": "slack_etl_disabled"}

    ingested = await _ingest_slack_event(request.app.state.db_pool, body.envelope)
    if ingested is None:
        return {"ok": True, "status": "ignored"}

    projected = False
    if body.project_context:
        projected = await _project_context_inline(
            request.app.state.db_pool,
            channel_id=ingested.channel_id,
            message_ts=ingested.message_ts,
        )

    log.info(
        "slack_event_ingested",
        channel_id=ingested.channel_id,
        message_ts=ingested.message_ts,
        action=ingested.action,
        projected=projected,
    )
    return {
        "ok": True,
        "status": "ingested",
        "action": ingested.action,
        "channel_id": ingested.channel_id,
        "message_ts": ingested.message_ts,
        "projected": projected,
    }
