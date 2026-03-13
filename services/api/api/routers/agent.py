"""Agent router — execute/stop/status/reconnect."""

from __future__ import annotations

import base64
import json as _json
import time as _time
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Any
from sse_starlette import EventSourceResponse, ServerSentEvent

from pydantic import BaseModel

from api.agent import (
    claim_for_delivery,
    get_or_spawn,
    get_status,
    list_undelivered,
    mark_delivered,
    stop_session,
    stream_exec,
    stream_reconnect,
)
from api.deps import require_scope, verify_api_key
from api.warm_pool import pool_status
from api.warm_pool import replenish as replenish_pool

log = structlog.get_logger()


router = APIRouter(
    prefix="/agent",
    tags=["agent"],
    dependencies=[Depends(verify_api_key)],
)


class ExecuteRequest(BaseModel):
    thread_key: str
    message: str | list[Any] = ""
    harness: str = "amp"
    engine: str | None = None
    platform: str | None = None
    user_id: str | None = None


@router.post("/execute", dependencies=[Depends(require_scope("agent:execute"))])
async def execute(request: Request):
    body = await request.json()
    thread_key = body.get("thread_key")
    if not thread_key:
        raise HTTPException(status_code=422, detail="thread_key is required")

    harness = body.get("harness", "amp")
    engine = body.get("engine")
    platform = body.get("platform")
    user_id = body.get("user_id")
    message = body.get("message", "")

    session = await get_or_spawn(thread_key, harness, engine=engine)

    return EventSourceResponse(
        stream_exec(session, message, platform=platform, user_id=user_id),
        ping_message_factory=lambda: ServerSentEvent(comment="keepalive"),
        sep="\n",
    )


async def _extract_attachments(
    pool, thread_key: str, message_id: str, parts: list[dict],
) -> list[dict]:
    """Replace inline base64 image/document parts with attachment_ref parts.

    Stores the binary data in the ``attachments`` table and returns a new parts
    list where each base64 blob is replaced by a lightweight reference.
    """
    out: list[dict] = []
    for part in parts:
        ptype = part.get("type")
        if ptype in ("image", "document"):
            source = part.get("source", {})
            if source.get("type") == "base64" and source.get("data"):
                att_id = f"att-{uuid.uuid4().hex[:16]}"
                mime_type = source.get("media_type", "application/octet-stream")
                raw_bytes = base64.b64decode(source["data"])
                name = part.get("name") or f"{ptype}.{mime_type.split('/')[-1]}"
                await pool.execute(
                    "INSERT INTO attachments (id, thread_key, message_id, name, mime_type, data) "
                    "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (id) DO NOTHING",
                    att_id, thread_key, message_id, name, mime_type, raw_bytes,
                )
                out.append({
                    "type": "attachment_ref",
                    "id": att_id,
                    "name": name,
                    "mime_type": mime_type,
                })
                log.info("attachment_stored", id=att_id, name=name, mime_type=mime_type, size=len(raw_bytes))
                continue
        out.append(part)
    return out


@router.post("/messages", dependencies=[Depends(require_scope("agent:execute"))])
async def post_messages(request: Request):
    """Buffer messages into chat_messages for a thread."""
    body = await request.json()
    thread_key = body.get("thread_key")
    if not thread_key:
        raise HTTPException(status_code=422, detail="thread_key is required")

    # Normalize: single message or batch
    raw_messages = body.get("messages")
    if raw_messages is None:
        # Single message request
        raw_messages = [{
            "role": body.get("role", "user"),
            "parts": body.get("parts", []),
            "user_id": body.get("user_id"),
            "metadata": body.get("metadata"),
        }]

    pool = request.app.state.db_pool
    inserted = 0
    for msg in raw_messages:
        parts = msg.get("parts", [])
        role = msg.get("role", "user")
        user_id = msg.get("user_id")
        metadata = msg.get("metadata") or {}

        # Generate deterministic ID from thread_key + slack_ts or timestamp
        slack_ts = metadata.get("slack_ts", "")
        if slack_ts:
            msg_id = f"{thread_key}-{slack_ts}"
        else:
            msg_id = f"{thread_key}-{int(_time.time() * 1000000)}"

        # Insert the message first (attachments FK references it), then
        # extract inline base64 blobs → attachments table → update parts.
        has_blobs = any(
            p.get("type") in ("image", "document")
            and p.get("source", {}).get("type") == "base64"
            for p in parts
        )

        result = await pool.execute(
            "INSERT INTO chat_messages (id, thread_key, role, parts, user_id, metadata) "
            "VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb) "
            "ON CONFLICT (id) DO NOTHING",
            msg_id,
            thread_key,
            role,
            _json.dumps(parts),
            user_id,
            _json.dumps(metadata),
        )
        if "INSERT 0 1" in result:
            inserted += 1

        if has_blobs:
            new_parts = await _extract_attachments(pool, thread_key, msg_id, parts)
            await pool.execute(
                "UPDATE chat_messages SET parts = $1::jsonb WHERE id = $2",
                _json.dumps(new_parts),
                msg_id,
            )

    log.info("message_buffered", thread_key=thread_key, message_count=len(raw_messages), inserted=inserted)
    return {"ok": True, "inserted": inserted}


@router.get("/messages", dependencies=[Depends(require_scope("agent:execute"))])
async def get_messages(request: Request, thread_key: str, cursor: str | None = None, limit: int = 50):
    """Paginated chat_messages for a thread."""
    pool = request.app.state.db_pool
    limit = min(limit, 200)

    if cursor:
        rows = await pool.fetch(
            "SELECT id, role, parts, user_id, metadata, created_at "
            "FROM chat_messages WHERE thread_key = $1 "
            "AND created_at > (SELECT created_at FROM chat_messages WHERE id = $2) "
            "ORDER BY created_at LIMIT $3",
            thread_key,
            cursor,
            limit + 1,
        )
    else:
        rows = await pool.fetch(
            "SELECT id, role, parts, user_id, metadata, created_at "
            "FROM chat_messages WHERE thread_key = $1 "
            "ORDER BY created_at LIMIT $2",
            thread_key,
            limit + 1,
        )

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    messages = []
    last_id = None
    for row in rows:
        last_id = row["id"]
        parts = row["parts"]
        if isinstance(parts, str):
            parts = _json.loads(parts)
        meta = row["metadata"]
        if isinstance(meta, str):
            meta = _json.loads(meta)
        messages.append({
            "id": row["id"],
            "role": row["role"],
            "parts": parts,
            "user_id": row["user_id"],
            "metadata": meta,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        })

    return {
        "messages": messages,
        "cursor": last_id if has_more else None,
        "has_more": has_more,
    }


class ReconnectRequest(BaseModel):
    thread_key: str
    harness: str = "amp"
    engine: str | None = None
    skip_done_count: int = 0


@router.post("/reconnect", dependencies=[Depends(require_scope("agent:execute"))])
async def reconnect(req: ReconnectRequest):
    """Re-attach to a running container's stdout without sending a new turn.

    Used by the slackbot to recover an in-progress stream after an API restart.
    Returns 404 if no running session exists for this thread.
    """
    session = await get_or_spawn(req.thread_key, req.harness, engine=req.engine)

    return EventSourceResponse(
        stream_reconnect(session, skip_done_count=req.skip_done_count),
        ping_message_factory=lambda: ServerSentEvent(comment="keepalive"),
        sep="\n",
    )


class StopRequest(BaseModel):
    thread_key: str


@router.post("/stop", dependencies=[Depends(require_scope("agent:stop"))])
async def stop(req: StopRequest):
    ok = await stop_session(req.thread_key)
    return {"ok": ok}


class TitleRequest(BaseModel):
    thread_key: str
    title: str


@router.post("/title", dependencies=[Depends(require_scope("agent:execute"))])
async def set_title(req: TitleRequest, request: Request):
    pool = request.app.state.db_pool
    await pool.execute(
        "UPDATE sandbox_sessions SET thread_name = $1, updated_at = NOW() WHERE thread_key = $2",
        req.title,
        req.thread_key,
    )
    return {"ok": True}


@router.get("/status", dependencies=[Depends(require_scope("agent:status"))])
async def status(request: Request, key: str):
    result = await get_status(key)
    # Add pending message count
    try:
        pool = request.app.state.db_pool
        session_row = await pool.fetchrow(
            "SELECT last_delivered_id FROM sandbox_sessions WHERE thread_key = $1", key
        )
        if session_row:
            last_id = session_row["last_delivered_id"]
            if last_id is None:
                count_row = await pool.fetchrow(
                    "SELECT COUNT(*) as cnt FROM chat_messages WHERE thread_key = $1", key
                )
            else:
                count_row = await pool.fetchrow(
                    "SELECT COUNT(*) as cnt FROM chat_messages WHERE thread_key = $1 "
                    "AND created_at > (SELECT created_at FROM chat_messages WHERE id = $2)",
                    key, last_id,
                )
            result["pending_messages"] = count_row["cnt"] if count_row else 0
        else:
            # No session yet — count all messages for this thread
            count_row = await pool.fetchrow(
                "SELECT COUNT(*) as cnt FROM chat_messages WHERE thread_key = $1", key
            )
            result["pending_messages"] = count_row["cnt"] if count_row else 0
    except Exception:
        pass
    return result


@router.get("/pool", dependencies=[Depends(require_scope("admin"))])
async def pool():
    """Return warm pool diagnostics."""
    return pool_status()


@router.post("/pool/replenish", dependencies=[Depends(require_scope("admin"))])
async def pool_replenish():
    """Manually trigger pool replenishment."""
    spawned = await replenish_pool()
    return {"spawned": spawned, **pool_status()}


@router.get("/orphaned", dependencies=[Depends(require_scope("agent:status"))])
async def list_orphaned(max_age_s: int = 300):
    """List threads that completed but may not have been delivered."""
    return await list_undelivered(max_age_s)


class MarkDeliveredRequest(BaseModel):
    thread_key: str


@router.post("/claim-delivery", dependencies=[Depends(require_scope("agent:execute"))])
async def claim_delivery_endpoint(req: MarkDeliveredRequest):
    """Atomically claim an idle session for delivery. Returns claimed=true if won the race."""
    claimed = await claim_for_delivery(req.thread_key)
    return {"claimed": claimed}


@router.post("/mark-delivered", dependencies=[Depends(require_scope("agent:execute"))])
async def mark_delivered_endpoint(req: MarkDeliveredRequest):
    """Mark a thread as delivered so it won't appear in orphan checks."""
    await mark_delivered(req.thread_key)
    return {"ok": True}


@router.get("/threads", dependencies=[Depends(require_scope("agent:status"))])
async def list_threads(request: Request, limit: int = 200):
    """List threads with summary info."""
    pool = request.app.state.db_pool
    limit = min(limit, 500)
    rows = await pool.fetch(
        """
        SELECT
            cm.thread_key,
            MIN(cm.created_at) AS created_at,
            MAX(cm.created_at) AS last_activity,
            COUNT(*)::int AS message_count,
            (SELECT parts FROM chat_messages cm2
             WHERE cm2.thread_key = cm.thread_key AND cm2.role = 'user'
             ORDER BY cm2.created_at ASC LIMIT 1) AS first_user_parts,
            (SELECT parts FROM chat_messages cm3
             WHERE cm3.thread_key = cm.thread_key AND cm3.role = 'user'
             ORDER BY cm3.created_at DESC LIMIT 1) AS last_user_parts,
            ss.thread_name,
            COALESCE(ss.harness, 'amp') AS harness,
            COALESCE(ss.state, 'stopped') AS state
        FROM chat_messages cm
        LEFT JOIN sandbox_sessions ss ON ss.thread_key = cm.thread_key
        GROUP BY cm.thread_key, ss.thread_name, ss.harness, ss.state
        ORDER BY MAX(cm.created_at) DESC
        LIMIT $1
        """,
        limit,
    )

    def _extract_text(parts):
        if isinstance(parts, str):
            try:
                parts = _json.loads(parts)
            except Exception:
                return None
        if not isinstance(parts, list):
            return None
        for p in parts:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                return p["text"]
        return None

    threads = []
    for row in rows:
        threads.append({
            "slack_thread_key": row["thread_key"],
            "harness": row["harness"],
            "state": row["state"],
            "created_at": row["created_at"].timestamp() if row["created_at"] else None,
            "last_activity": row["last_activity"].timestamp() if row["last_activity"] else None,
            "turn_count": row["message_count"],
            "first_message": _extract_text(row["first_user_parts"]),
            "last_user_message": _extract_text(row["last_user_parts"]),
            "thread_name": row["thread_name"],
        })

    return {"threads": threads}


@router.get("/threads/detail", dependencies=[Depends(require_scope("agent:status"))])
async def thread_detail(request: Request, key: str):
    """Get detailed info for a single thread."""
    pool = request.app.state.db_pool

    rows = await pool.fetch(
        """
        SELECT
            MIN(cm.created_at) AS created_at,
            MAX(cm.created_at) AS last_activity,
            COUNT(*)::int AS message_count,
            (SELECT parts FROM chat_messages cm2
             WHERE cm2.thread_key = $1 AND cm2.role = 'user'
             ORDER BY cm2.created_at DESC LIMIT 1
            ) AS last_user_parts,
            ss.thread_name,
            COALESCE(ss.harness, 'amp') AS harness,
            COALESCE(ss.state, 'stopped') AS state
        FROM chat_messages cm
        LEFT JOIN sandbox_sessions ss ON ss.thread_key = cm.thread_key
        WHERE cm.thread_key = $1
        GROUP BY ss.thread_name, ss.harness, ss.state
        """,
        key,
    )

    if not rows:
        raise HTTPException(status_code=404, detail=f"Thread not found: {key}")

    row = rows[0]

    def _extract_text(parts):
        if isinstance(parts, str):
            try:
                parts = _json.loads(parts)
            except Exception:
                return None
        if not isinstance(parts, list):
            return None
        for p in parts:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                return p["text"]
        return None

    detail = {
        "slack_thread_key": key,
        "harness": row["harness"],
        "state": row["state"],
        "created_at": row["created_at"].timestamp() if row["created_at"] else None,
        "last_activity": row["last_activity"].timestamp() if row["last_activity"] else None,
        "message_count": row["message_count"],
        "last_user_message": _extract_text(row["last_user_parts"]),
        "thread_name": row["thread_name"],
    }

    # Collect participants and token usage from message rows
    msg_rows = await pool.fetch(
        "SELECT parts, metadata FROM chat_messages "
        "WHERE thread_key = $1 ORDER BY created_at DESC LIMIT 200",
        key,
    )

    participants = {}
    total_tokens = 0
    total_input = 0
    total_output = 0
    total_cost = 0.0
    has_usage = False
    models = set()

    for mrow in msg_rows:
        parts = mrow["parts"]
        if isinstance(parts, str):
            try:
                parts = _json.loads(parts)
            except Exception:
                parts = []
        meta = mrow["metadata"]
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except Exception:
                meta = None

        # Participants
        if meta and isinstance(meta, dict):
            uid = meta.get("user_id")
            if uid and isinstance(uid, str) and uid.strip():
                uid = uid.strip()
                if uid not in participants:
                    participants[uid] = {
                        "id": uid,
                        "name": meta.get("user_name") or meta.get("name") or uid,
                        "username": meta.get("username"),
                        "avatar_url": meta.get("avatar_url"),
                    }
            # Token usage from metadata
            tu = meta.get("token_usage")
            if tu and isinstance(tu, dict):
                t = tu.get("total_tokens", 0)
                if isinstance(t, (int, float)) and t > 0:
                    has_usage = True
                    total_tokens += int(t)
                    inp = tu.get("input_tokens")
                    if isinstance(inp, (int, float)):
                        total_input += int(inp)
                    out = tu.get("output_tokens")
                    if isinstance(out, (int, float)):
                        total_output += int(out)
                    c = tu.get("cost_usd")
                    if isinstance(c, (int, float)):
                        total_cost += c
                    for m in (tu.get("models") or []):
                        if isinstance(m, str):
                            models.add(m)

        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "data-user-message" or part.get("type") == "data-context-message":
                    data = part.get("data") or {}
                    uid = data.get("user_id")
                    if uid and isinstance(uid, str) and uid.strip():
                        uid = uid.strip()
                        if uid not in participants:
                            participants[uid] = {
                                "id": uid,
                                "name": data.get("user_name") or data.get("name") or uid,
                                "username": data.get("username"),
                                "avatar_url": data.get("avatar_url"),
                            }
                if part.get("type") == "data-token-usage":
                    tu = part.get("data") or {}
                    t = tu.get("total_tokens", 0)
                    if isinstance(t, (int, float)) and t > 0:
                        has_usage = True
                        total_tokens += int(t)
                        inp = tu.get("input_tokens")
                        if isinstance(inp, (int, float)):
                            total_input += int(inp)
                        out = tu.get("output_tokens")
                        if isinstance(out, (int, float)):
                            total_output += int(out)
                        c = tu.get("cost_usd")
                        if isinstance(c, (int, float)):
                            total_cost += c
                        for m in (tu.get("models") or []):
                            if isinstance(m, str):
                                models.add(m)

    detail["participants"] = list(participants.values())
    detail["token_usage"] = {
        "total_tokens": total_tokens,
        "input_tokens": total_input or None,
        "output_tokens": total_output or None,
        "cost_usd": total_cost if total_cost > 0 else None,
        "models": sorted(models),
    } if has_usage else None

    return detail


# ── Internal DB query (read-only) ───────────────────────────────────────────

_ALLOWED_TABLES = {"chat_messages", "sandbox_sessions", "attachments", "api_keys"}
_BLOCKED_PATTERNS = {"drop ", "delete ", "insert ", "update ", "alter ", "create ", "truncate ", "grant ", "revoke "}


@router.post("/query", dependencies=[Depends(verify_api_key)])
async def query_db(request: Request):
    """Run a read-only SQL query against Centaur's own database.

    Only SELECT on allowed tables. Returns rows as JSON.
    """
    body = await request.json()
    sql = (body.get("sql") or "").strip()
    if not sql:
        raise HTTPException(status_code=422, detail="sql is required")

    sql_lower = sql.lower()
    if not sql_lower.startswith("select"):
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed")
    for pat in _BLOCKED_PATTERNS:
        if pat in sql_lower:
            raise HTTPException(status_code=400, detail=f"Query contains blocked keyword: {pat.strip()}")

    pool = request.app.state.db_pool
    try:
        rows = await pool.fetch(sql)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Convert to JSON-serializable dicts
    results = []
    for row in rows[:500]:
        record = {}
        for key, val in row.items():
            if isinstance(val, (bytes, bytearray, memoryview)):
                record[key] = f"<{len(val)} bytes>"
            elif hasattr(val, "isoformat"):
                record[key] = val.isoformat()
            else:
                record[key] = val
        results.append(record)

    return {"rows": results, "count": len(results)}
