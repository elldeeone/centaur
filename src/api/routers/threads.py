"""Thread list, detail, messages, and persistence — served from chat_messages in Postgres."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from api.deps import verify_ui_or_api_key

router = APIRouter(
    prefix="/threads",
    tags=["threads"],
    dependencies=[Depends(verify_ui_or_api_key)],
)


def _extract_text(parts: list | None) -> str | None:
    """Extract the first text string from a parts JSONB array."""
    if not isinstance(parts, list):
        return None
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return part["text"]
    return None


@router.get("")
async def list_threads(request: Request):
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        """
        SELECT
            thread_key,
            MIN(created_at) AS created_at,
            MAX(created_at) AS last_activity,
            COUNT(*) AS message_count,
            (SELECT parts FROM chat_messages cm2
             WHERE cm2.thread_key = cm.thread_key AND cm2.role = 'user'
             ORDER BY cm2.created_at ASC LIMIT 1) AS first_user_parts,
            (SELECT parts FROM chat_messages cm3
             WHERE cm3.thread_key = cm.thread_key AND cm3.role = 'user'
             ORDER BY cm3.created_at DESC LIMIT 1) AS last_user_parts,
            (SELECT metadata->>'thread_name' FROM chat_messages cm4
             WHERE cm4.thread_key = cm.thread_key AND cm4.metadata->>'thread_name' IS NOT NULL
             ORDER BY cm4.created_at DESC LIMIT 1) AS thread_name
        FROM chat_messages cm
        GROUP BY thread_key
        ORDER BY MAX(created_at) DESC
        LIMIT 200
        """
    )

    threads = []
    for row in rows:
        threads.append(
            {
                "slack_thread_key": row["thread_key"],
                "harness": "amp",
                "state": "idle",
                "created_at": row["created_at"].timestamp(),
                "last_activity": row["last_activity"].timestamp(),
                "turn_count": row["message_count"],
                "first_message": _extract_text(row["first_user_parts"]),
                "last_user_message": _extract_text(row["last_user_parts"]),
                "thread_name": row["thread_name"],
            }
        )

    return JSONResponse(
        {"threads": threads},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/detail")
async def thread_detail(request: Request, key: str = Query(...)):
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        """
        SELECT
            MIN(created_at) AS created_at,
            MAX(created_at) AS last_activity,
            COUNT(*)::text AS message_count,
            (SELECT parts FROM chat_messages cm2
             WHERE cm2.thread_key = $1 AND cm2.role = 'user'
             ORDER BY cm2.created_at DESC LIMIT 1
            ) AS last_user_parts,
            (SELECT metadata->>'thread_name' FROM chat_messages cm3
             WHERE cm3.thread_key = $1 AND cm3.metadata->>'thread_name' IS NOT NULL
             ORDER BY cm3.created_at DESC LIMIT 1
            ) AS thread_name
        FROM chat_messages
        WHERE thread_key = $1
        """,
        key,
    )

    if row is None or row["created_at"] is None:
        return JSONResponse(
            {"error": f"Thread not found: {key}"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    detail = {
        "slack_thread_key": key,
        "harness": "amp",
        "state": "idle",
        "created_at": row["created_at"].timestamp(),
        "last_activity": row["last_activity"].timestamp(),
        "message_count": int(row["message_count"]),
        "last_user_message": _extract_text(row["last_user_parts"]),
        "thread_name": row["thread_name"],
        "token_usage": None,
    }

    return JSONResponse(detail, headers={"Cache-Control": "no-store"})


@router.get("/messages")
async def thread_messages(request: Request, key: str = Query(...)):
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        "SELECT id, role, parts, created_at, metadata "
        "FROM chat_messages WHERE thread_key = $1 ORDER BY created_at",
        key,
    )

    messages = []
    for row in rows:
        messages.append(
            {
                "id": row["id"],
                "role": row["role"],
                "parts": row["parts"],
                "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
                "metadata": row["metadata"],
            }
        )

    return JSONResponse(messages, headers={"Cache-Control": "no-store"})


class ChatMessage(BaseModel):
    id: str
    role: str
    parts: Any
    metadata: dict[str, Any] = {}


class PersistMessagesRequest(BaseModel):
    thread_key: str
    messages: list[ChatMessage]


@router.post("/messages")
async def persist_messages(request: Request, body: PersistMessagesRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn, conn.transaction():
        for msg in body.messages:
            await conn.execute(
                """
                    INSERT INTO chat_messages (id, thread_key, role, parts, metadata)
                    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
                    ON CONFLICT (id) DO UPDATE SET parts = $4::jsonb, metadata = $5::jsonb
                    """,
                msg.id,
                body.thread_key,
                msg.role,
                msg.parts if isinstance(msg.parts, str) else json.dumps(msg.parts),
                json.dumps(msg.metadata),
            )
    return JSONResponse({"ok": True})
