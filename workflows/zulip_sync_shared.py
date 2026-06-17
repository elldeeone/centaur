"""Shared helpers for Zulip ETL incremental sync and backfill workflows."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, ClassVar

from centaur_sdk import secret

from api.runtime_control import canonical_json

FALSE_ENV_VALUES = {"0", "false", "no", "off"}
BACKFILL_JOB_STREAM_CONTINUATION = "stream_continuation"
BACKFILL_JOB_STREAM_BOOTSTRAP = "stream_bootstrap"
BACKFILL_JOB_PAYLOAD_VERSION = 1


def positive_int(value: int | str | None, default: int) -> int:
    """Coerce positive integer config values with a safe default."""
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def env_flag_enabled(name: str, default: bool = True) -> bool:
    """Read a boolean feature flag where common false strings opt out."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in FALSE_ENV_VALUES


def _secret_or_env(name: str, *, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return value
    return str(secret(name, default=default) or "")


def zulip_ts_to_datetime(ts: int | float | str | None) -> dt.datetime | None:
    """Convert Zulip epoch timestamps to UTC datetimes for indexed queries."""
    if ts in (None, ""):
        return None
    try:
        seconds = float(ts)
        if seconds >= 1_000_000_000_000:
            seconds /= 1000.0
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def message_topic(message: dict[str, Any]) -> str:
    """Return the topic name from either modern or legacy Zulip fields."""
    return str(message.get("topic") or message.get("subject") or "").strip()


def stream_ref(
    stream: dict[str, Any],
    *,
    realm: str,
    reason: str | None = None,
) -> dict[str, str]:
    """Return a compact stream reference for run summaries."""
    result = {
        "realm": realm,
        "stream_id": str(stream.get("stream_id") or ""),
        "stream_name": str(stream.get("name") or ""),
    }
    if reason:
        result["reason"] = reason
    return result


def message_row(
    message: dict[str, Any],
    run_id: str,
    *,
    realm: str,
    stream_id: int | None = None,
) -> dict[str, Any]:
    """Project a Zulip message into the DB upsert shape."""
    raw_stream_id = message.get("stream_id", stream_id)
    return {
        "realm": realm,
        "message_id": int(message.get("id") or 0),
        "stream_id": int(raw_stream_id or 0),
        "topic_name": message_topic(message),
        "occurred_at": zulip_ts_to_datetime(message.get("timestamp")),
        "sender_id": (
            int(message["sender_id"])
            if message.get("sender_id") not in (None, "")
            else None
        ),
        "sender_email": str(message.get("sender_email") or ""),
        "sender_full_name": str(message.get("sender_full_name") or ""),
        "recipient_id": (
            int(message["recipient_id"])
            if message.get("recipient_id") not in (None, "")
            else None
        ),
        "message_type": str(message.get("type") or "stream"),
        "content": str(message.get("content") or ""),
        "rendered_content": str(message.get("rendered_content") or ""),
        "subject": str(message.get("subject") or ""),
        "permalink": str(message.get("permalink") or ""),
        "raw_payload": message,
        "source_run_id": run_id,
    }


def failure_reason(error: str) -> str:
    """Map Zulip/client errors to low-cardinality metric reasons."""
    lowered = error.lower()
    if "rate_limited" in lowered or "429" in lowered:
        return "rate_limited"
    if "permission" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
        return "permission_error"
    if "cursor" in lowered or "anchor" in lowered:
        return "cursor_error"
    if "zulip api" in lowered:
        return "api_error"
    if "write" in lowered or "database" in lowered or "postgres" in lowered:
        return "write_error"
    return "unknown_error"


async def upsert_messages(pool, rows: list[dict[str, Any]]) -> int:
    """Upsert Zulip stream messages by their realm-scoped message id."""
    if not rows:
        return 0
    upserted = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for row in rows:
                if not row["message_id"] or not row["stream_id"]:
                    continue
                await conn.execute(
                    "INSERT INTO zulip_sync_messages ("
                    "realm, message_id, stream_id, topic_name, occurred_at, sender_id, "
                    "sender_email, sender_full_name, recipient_id, message_type, content, "
                    "rendered_content, subject, permalink, raw_payload, source_run_id, "
                    "last_seen_at, updated_at"
                    ") VALUES ("
                    "$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, "
                    "$15::jsonb, $16, NOW(), NOW()"
                    ") ON CONFLICT (realm, message_id) DO UPDATE SET "
                    "stream_id = EXCLUDED.stream_id, "
                    "topic_name = EXCLUDED.topic_name, "
                    "occurred_at = EXCLUDED.occurred_at, "
                    "sender_id = EXCLUDED.sender_id, "
                    "sender_email = EXCLUDED.sender_email, "
                    "sender_full_name = EXCLUDED.sender_full_name, "
                    "recipient_id = EXCLUDED.recipient_id, "
                    "message_type = EXCLUDED.message_type, "
                    "content = EXCLUDED.content, "
                    "rendered_content = EXCLUDED.rendered_content, "
                    "subject = EXCLUDED.subject, "
                    "permalink = EXCLUDED.permalink, "
                    "raw_payload = EXCLUDED.raw_payload, "
                    "source_run_id = EXCLUDED.source_run_id, "
                    "last_seen_at = NOW(), "
                    "updated_at = NOW()",
                    row["realm"],
                    row["message_id"],
                    row["stream_id"],
                    row["topic_name"],
                    row["occurred_at"],
                    row["sender_id"],
                    row["sender_email"],
                    row["sender_full_name"],
                    row["recipient_id"],
                    row["message_type"],
                    row["content"],
                    row["rendered_content"],
                    row["subject"],
                    row["permalink"],
                    canonical_json(row["raw_payload"]),
                    row["source_run_id"],
                )
                upserted += 1
    return upserted


async def load_stream_checkpoints(
    pool,
    *,
    realm: str,
    stream_ids: list[int],
) -> dict[int, int | None]:
    """Load last synced message ids keyed by Zulip stream id."""
    if not stream_ids:
        return {}
    rows = await pool.fetch(
        "SELECT stream_id, watermark_message_id "
        "FROM zulip_sync_checkpoints "
        "WHERE realm = $1 "
        "  AND stream_id = ANY($2::bigint[])",
        realm,
        stream_ids,
    )
    return {
        int(row["stream_id"]): (
            int(row["watermark_message_id"])
            if row["watermark_message_id"] is not None
            else None
        )
        for row in rows
    }


async def update_checkpoint(
    pool,
    *,
    realm: str,
    stream_id: int,
    watermark_message_id: int | None,
    run_id: str,
    error: str = "",
) -> None:
    """Upsert the stream checkpoint after a sync attempt."""
    await pool.execute(
        "INSERT INTO zulip_sync_checkpoints ("
        "realm, stream_id, watermark_message_id, last_run_id, last_success_at, "
        "last_error, updated_at"
        ") VALUES ($1, $2, $3, $4, CASE WHEN $5 = '' THEN NOW() ELSE NULL END, $5, NOW()) "
        "ON CONFLICT (realm, stream_id) DO UPDATE SET "
        "watermark_message_id = COALESCE(EXCLUDED.watermark_message_id, zulip_sync_checkpoints.watermark_message_id), "
        "last_run_id = EXCLUDED.last_run_id, "
        "last_success_at = CASE WHEN EXCLUDED.last_error = '' THEN NOW() ELSE zulip_sync_checkpoints.last_success_at END, "
        "last_error = EXCLUDED.last_error, "
        "updated_at = NOW()",
        realm,
        stream_id,
        watermark_message_id,
        run_id,
        error,
    )


async def record_run_start(
    pool,
    *,
    run_id: str,
    workflow_run_id: str,
    mode: str,
    realm: str,
    requested: list[dict[str, str]],
    skipped: list[dict[str, str]],
    metadata: dict[str, Any],
) -> None:
    """Insert or reset the ETL run row."""
    await pool.execute(
        "INSERT INTO zulip_sync_runs ("
        "run_id, workflow_run_id, mode, status, realm, streams_requested, streams_skipped, metadata"
        ") VALUES ($1, $2, $3, 'running', $4, $5::jsonb, $6::jsonb, $7::jsonb) "
        "ON CONFLICT (run_id) DO UPDATE SET "
        "workflow_run_id = EXCLUDED.workflow_run_id, "
        "mode = EXCLUDED.mode, "
        "status = 'running', "
        "realm = EXCLUDED.realm, "
        "streams_requested = EXCLUDED.streams_requested, "
        "streams_synced = '[]'::jsonb, "
        "streams_skipped = EXCLUDED.streams_skipped, "
        "streams_failed = '[]'::jsonb, "
        "topics_fetched = 0, "
        "messages_fetched = 0, "
        "messages_upserted = 0, "
        "finished_at = NULL, "
        "error_text = '', "
        "metadata = EXCLUDED.metadata",
        run_id,
        workflow_run_id,
        mode,
        realm,
        canonical_json(requested),
        canonical_json(skipped),
        canonical_json(metadata),
    )


async def record_run_finish(
    pool,
    *,
    run_id: str,
    status: str,
    synced: list[dict[str, str]],
    skipped: list[dict[str, str]],
    failed: list[dict[str, str]],
    counts: dict[str, int],
    error_text: str = "",
) -> None:
    """Finalize a sync run with stream outcomes and row counts."""
    await pool.execute(
        "UPDATE zulip_sync_runs SET "
        "status = $2, streams_synced = $3::jsonb, streams_skipped = $4::jsonb, "
        "streams_failed = $5::jsonb, topics_fetched = $6, messages_fetched = $7, "
        "messages_upserted = $8, finished_at = NOW(), error_text = $9 "
        "WHERE run_id = $1",
        run_id,
        status,
        canonical_json(synced),
        canonical_json(skipped),
        canonical_json(failed),
        counts.get("topics_fetched", 0),
        counts.get("messages_fetched", 0),
        counts.get("messages_upserted", 0),
        error_text,
    )


def workflow_run_id_to_sync_run_id(workflow_run_id: str) -> str:
    """Derive a stable sync run id from the durable workflow run id."""
    safe_run_id = "".join(char if char.isalnum() else "_" for char in workflow_run_id)
    return f"zulip_sync_{safe_run_id}"


async def enqueue_backfill_job(
    pool,
    *,
    job_key: str,
    job_type: str,
    realm: str,
    stream_id: int,
    topic_name: str = "",
    payload: dict[str, Any],
    priority: int = 100,
) -> None:
    """Insert or refresh a resumable Zulip backfill job."""
    await pool.execute(
        "INSERT INTO zulip_sync_backfill_jobs ("
        "job_key, job_type, payload_version, realm, stream_id, topic_name, status, "
        "payload_json, priority, last_enqueued_at, updated_at"
        ") VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7::jsonb, $8, NOW(), NOW()) "
        "ON CONFLICT (job_key) DO UPDATE SET "
        "job_type = EXCLUDED.job_type, "
        "payload_version = EXCLUDED.payload_version, "
        "realm = EXCLUDED.realm, "
        "stream_id = EXCLUDED.stream_id, "
        "topic_name = EXCLUDED.topic_name, "
        "status = CASE "
        "    WHEN zulip_sync_backfill_jobs.status = 'running' THEN 'running' "
        "    ELSE 'pending' "
        "END, "
        "payload_json = EXCLUDED.payload_json, "
        "priority = LEAST(zulip_sync_backfill_jobs.priority, EXCLUDED.priority), "
        "last_enqueued_at = NOW(), "
        "updated_at = NOW()",
        job_key,
        job_type,
        BACKFILL_JOB_PAYLOAD_VERSION,
        realm,
        stream_id,
        topic_name,
        canonical_json(payload),
        priority,
    )


async def seed_stream_bootstrap_job(
    pool,
    *,
    realm: str,
    stream_id: int,
    lookback_days: int,
) -> None:
    """Create a stable initial backfill job for one stream."""
    await enqueue_backfill_job(
        pool,
        job_key=f"bootstrap:{realm}:{stream_id}",
        job_type=BACKFILL_JOB_STREAM_BOOTSTRAP,
        realm=realm,
        stream_id=stream_id,
        payload={"anchor": "newest", "lookback_days": lookback_days},
        priority=200,
    )


async def claim_backfill_jobs(pool, limit: int) -> list[dict[str, Any]]:
    """Claim a bounded batch of pending Zulip backfill jobs."""
    rows = await pool.fetch(
        "WITH picked AS ("
        "    SELECT job_id "
        "    FROM zulip_sync_backfill_jobs "
        "    WHERE status = 'pending' "
        "    ORDER BY priority ASC, updated_at ASC "
        "    LIMIT $1 "
        "    FOR UPDATE SKIP LOCKED"
        ") "
        "UPDATE zulip_sync_backfill_jobs jobs SET "
        "status = 'running', attempt_count = attempt_count + 1, "
        "last_started_at = NOW(), updated_at = NOW() "
        "FROM picked "
        "WHERE jobs.job_id = picked.job_id "
        "RETURNING jobs.*",
        limit,
    )
    return [dict(row) for row in rows]


async def mark_backfill_job_completed(pool, job_id: int) -> None:
    """Mark a backfill job as completed."""
    await pool.execute(
        "UPDATE zulip_sync_backfill_jobs SET "
        "status = 'completed', last_completed_at = NOW(), last_error = '', updated_at = NOW() "
        "WHERE job_id = $1",
        job_id,
    )


async def mark_backfill_job_failed(pool, job_id: int, error: str) -> None:
    """Mark a backfill job as retryable after failure."""
    await pool.execute(
        "UPDATE zulip_sync_backfill_jobs SET "
        "status = 'pending', last_error = $2, updated_at = NOW() "
        "WHERE job_id = $1",
        job_id,
        error[:1000],
    )


class ZulipEtlAuthError(RuntimeError):
    """Structured Zulip ETL auth failure."""

    def __init__(self, *, zulip_method: str, status_code: int | None, error: str) -> None:
        payload = {
            "error": "zulip_auth_failed",
            "message": f"Zulip authentication failed for {zulip_method}",
            "zulip_method": zulip_method,
            "status_code": status_code,
            "error_code": error,
        }
        self.payload = payload
        super().__init__(json.dumps(payload, sort_keys=True))


class ZulipEtlRateLimitError(RuntimeError):
    """Structured Zulip ETL rate-limit failure."""

    def __init__(self, *, zulip_method: str, retry_after: float) -> None:
        payload = {
            "error": "zulip_rate_limited",
            "message": f"Zulip rate limited {zulip_method}; retry after {retry_after:.2f}s",
            "zulip_method": zulip_method,
            "retry_after_seconds": retry_after,
        }
        self.payload = payload
        super().__init__(json.dumps(payload, sort_keys=True))


class ZulipEtlClient:
    """Zulip bot-token client used only by Zulip ETL workflows."""

    _DEFAULT_API_TIMEOUT_SECONDS = 8
    _MAX_RATE_LIMIT_SLEEP_SECONDS = 30.0
    _MAX_PAGE_SIZE = 1000
    _AUTH_STATUS_CODES: ClassVar[frozenset[int]] = frozenset({401, 403})

    def __init__(
        self,
        *,
        site: str | None = None,
        email: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.site = (
            site
            or _secret_or_env("ZULIP_ETL_SITE")
            or _secret_or_env("ZULIP_SITE")
        ).rstrip("/")
        self.email = (
            email
            or _secret_or_env("ZULIP_ETL_EMAIL")
            or _secret_or_env("ZULIP_BOT_EMAIL")
        ).strip()
        self.api_key = (
            api_key
            or _secret_or_env("ZULIP_ETL_API_KEY")
            or _secret_or_env("ZULIP_API_KEY")
        ).strip()
        if not self.site:
            raise RuntimeError("ZULIP_ETL_SITE or ZULIP_SITE not set for Zulip ETL workflow")
        if not self.email:
            raise RuntimeError("ZULIP_ETL_EMAIL or ZULIP_BOT_EMAIL not set for Zulip ETL workflow")
        if not self.api_key:
            raise RuntimeError("ZULIP_ETL_API_KEY or ZULIP_API_KEY not set for Zulip ETL workflow")
        self.realm = urllib.parse.urlparse(self.site).hostname or self.site
        self._ratelimit_deadlines: dict[str, float] = {}

    def _api_timeout_seconds(self) -> int:
        raw = _secret_or_env("ZULIP_API_TIMEOUT_SECONDS")
        if not raw:
            return self._DEFAULT_API_TIMEOUT_SECONDS
        try:
            return max(1, int(raw))
        except ValueError:
            return self._DEFAULT_API_TIMEOUT_SECONDS

    def _authorization_header(self) -> str:
        raw = f"{self.email}:{self.api_key}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method_key: str,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{self.site}{path}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Authorization": self._authorization_header(),
                "Accept": "application/json",
                "User-Agent": "centaur-zulip-etl/1.0",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._api_timeout_seconds()
            ) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            error_code = self._zulip_error_code(body)
            if exc.code == 429:
                retry_after = self._parse_retry_after(exc.headers.get("Retry-After"))
                raise ZulipEtlRateLimitError(
                    zulip_method=method_key,
                    retry_after=retry_after,
                ) from exc
            if exc.code in self._AUTH_STATUS_CODES:
                raise ZulipEtlAuthError(
                    zulip_method=method_key,
                    status_code=exc.code,
                    error=error_code,
                ) from exc
            raise RuntimeError(f"Zulip API error: {exc.code} {error_code}") from exc

        data = json.loads(payload)
        if data.get("result") == "error":
            raise RuntimeError(f"Zulip API error: {data.get('code') or data.get('msg')}")
        return data

    def _retry_on_ratelimit(self, func, *args, method_key: str, **kwargs):
        max_sleep = self._MAX_RATE_LIMIT_SLEEP_SECONDS
        for attempt in range(6):
            blocked_until = self._ratelimit_deadlines.get(method_key, 0.0)
            remaining = blocked_until - time.time()
            if remaining > 0:
                if remaining > max_sleep:
                    raise ZulipEtlRateLimitError(
                        zulip_method=method_key,
                        retry_after=round(remaining, 3),
                    )
                time.sleep(remaining)
            try:
                return func(*args, method_key=method_key, **kwargs)
            except ZulipEtlRateLimitError as exc:
                retry_after = float(exc.payload.get("retry_after_seconds") or 1.0)
                self._ratelimit_deadlines[method_key] = time.time() + retry_after
                if attempt < 5 and retry_after <= max_sleep:
                    time.sleep(retry_after)
                    continue
                raise
        raise RuntimeError("Max retries exceeded")

    def _parse_retry_after(self, value: str | None, default: int = 5) -> float:
        try:
            seconds = float(value) if value is not None else float(default)
        except (TypeError, ValueError):
            seconds = float(default)
        return max(seconds, 1.0) + 0.25

    def _zulip_error_code(self, body: str) -> str:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return "unknown_error"
        return str(data.get("code") or data.get("msg") or "unknown_error")

    def _etl_access_mode(self) -> str:
        return "bot_token"

    def _list_etl_streams(self) -> list[dict[str, Any]]:
        data = self._retry_on_ratelimit(
            self._request,
            "GET",
            "/api/v1/streams",
            params={
                "include_public": "true",
                "include_web_public": "true",
                "include_subscribed": "true",
                "include_all_active": "true",
            },
            method_key="streams",
        )
        return list(data.get("streams") or [])

    def _list_etl_users(self) -> list[dict[str, Any]]:
        data = self._retry_on_ratelimit(
            self._request,
            "GET",
            "/api/v1/users",
            params={"client_gravatar": "false"},
            method_key="users",
        )
        return list(data.get("members") or [])

    def _list_stream_topics(self, stream_id: int) -> list[dict[str, Any]]:
        data = self._retry_on_ratelimit(
            self._request,
            "GET",
            f"/api/v1/users/me/{stream_id}/topics",
            method_key="stream_topics",
        )
        return list(data.get("topics") or [])

    def _get_messages_page(
        self,
        *,
        stream_id: int,
        topic_name: str | None = None,
        anchor: str | int = "newest",
        num_before: int = 0,
        num_after: int = 0,
    ) -> dict[str, Any]:
        narrow = [{"operator": "stream", "operand": stream_id}]
        if topic_name:
            narrow.append({"operator": "topic", "operand": topic_name})
        return self._retry_on_ratelimit(
            self._request,
            "GET",
            "/api/v1/messages",
            params={
                "anchor": anchor,
                "num_before": min(max(num_before, 0), self._MAX_PAGE_SIZE),
                "num_after": min(max(num_after, 0), self._MAX_PAGE_SIZE),
                "narrow": json.dumps(narrow, separators=(",", ":")),
                "apply_markdown": "true",
            },
            method_key="messages",
        )

    def _sync_etl_stream_history(
        self,
        *,
        stream_id: int,
        state: dict[str, Any] | None = None,
        limit: int = 200,
        lookback_days: int = 30,
        topic_name: str | None = None,
    ) -> dict[str, Any]:
        state = state or {}
        anchor = state.get("anchor")
        if anchor not in (None, "") and anchor != "newest":
            page = self._get_messages_page(
                stream_id=stream_id,
                topic_name=topic_name,
                anchor=int(anchor),
                num_before=0,
                num_after=limit,
            )
        else:
            page = self._get_messages_page(
                stream_id=stream_id,
                topic_name=topic_name,
                anchor="newest",
                num_before=limit,
                num_after=0,
            )
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
            page["messages"] = [
                message
                for message in page.get("messages", [])
                if (zulip_ts_to_datetime(message.get("timestamp")) or cutoff) >= cutoff
            ]
        messages = [self._decorate_message(message) for message in page.get("messages", [])]
        if messages:
            next_anchor = max(int(message.get("id") or 0) for message in messages)
        else:
            next_anchor = state.get("anchor")
        return {
            "messages": messages,
            "next_state": {"anchor": next_anchor},
            "found_oldest": bool(page.get("found_oldest")),
            "found_newest": bool(page.get("found_newest")),
        }

    def _backfill_stream_history(
        self,
        *,
        stream_id: int,
        anchor: str | int = "newest",
        limit: int = 200,
        topic_name: str | None = None,
    ) -> dict[str, Any]:
        page = self._get_messages_page(
            stream_id=stream_id,
            topic_name=topic_name,
            anchor=anchor,
            num_before=limit,
            num_after=0,
        )
        messages = [self._decorate_message(message) for message in page.get("messages", [])]
        next_anchor = min((int(message.get("id") or 0) for message in messages), default=None)
        return {
            "messages": messages,
            "next_anchor": next_anchor,
            "found_oldest": bool(page.get("found_oldest")),
        }

    def _decorate_message(self, message: dict[str, Any]) -> dict[str, Any]:
        message = dict(message)
        message.setdefault("permalink", self._message_permalink(message))
        return message

    def _message_permalink(self, message: dict[str, Any]) -> str:
        message_id = message.get("id")
        stream_id = message.get("stream_id")
        topic_name = message_topic(message)
        if stream_id and topic_name:
            encoded_topic = urllib.parse.quote(topic_name, safe="")
            return f"{self.site}/#narrow/channel/{stream_id}/topic/{encoded_topic}/near/{message_id}"
        return f"{self.site}/#narrow/near/{message_id}"


def client() -> ZulipEtlClient:
    """Build the default Zulip ETL client."""
    return ZulipEtlClient()
