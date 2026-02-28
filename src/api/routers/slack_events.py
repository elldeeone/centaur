from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import shutil
import time
from collections import OrderedDict

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from api.deps import verify_api_key
from shared.engineer.models import Phase
from shared.engineer.orchestrator import EngineerOrchestrator
from shared.engineer.session import (
    create_session,
    get_session,
    has_active_session,
    register_task,
    remove_session,
)
from shared.engineer.settings import EngineerSettings, engineer_settings

router = APIRouter(prefix="/slack")
log = structlog.get_logger()

_MENTION_RE = re.compile(r"<@[^>]+>")
_seen_events: OrderedDict[str, float] = OrderedDict()
_seen_events_lock = asyncio.Lock()
_SEEN_EVENT_TTL_SECONDS = 3600.0
_MAX_SEEN_EVENTS = 4000
_MAX_SLACK_MESSAGE_CHARS = 3800
_ENG_FLAG_RE = re.compile(r"(^|\s)--eng(?=\s|$)", re.IGNORECASE)
_HARNESS_EQ_RE = re.compile(r"\bharness\s*=\s*(amp|claude-code|codex|pi-mono)\b", re.IGNORECASE)
_ENGINE_FLAG_RE = re.compile(
    r"(^|\s)--engine\s+(amp|claude-code|codex|pi-mono)(?=\s|$)", re.IGNORECASE
)
_MODEL_EQ_RE = re.compile(r"\bmodel\s*=\s*([A-Za-z0-9._-]+)\b", re.IGNORECASE)
_MODEL_FLAG_RE = re.compile(r"(^|\s)--model\s+([A-Za-z0-9._-]+)(?=\s|$)", re.IGNORECASE)
_MODEL_FLAG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|\s)--amp(?=\s|$)", re.IGNORECASE), "amp"),
    (re.compile(r"(^|\s)--claude(?=\s|$)", re.IGNORECASE), "claude-code"),
    (re.compile(r"(^|\s)--claude-code(?=\s|$)", re.IGNORECASE), "claude-code"),
    (re.compile(r"(^|\s)--codex(?=\s|$)", re.IGNORECASE), "codex"),
    (re.compile(r"(^|\s)--pi(?=\s|$)", re.IGNORECASE), "pi-mono"),
    (re.compile(r"(^|\s)--pi-mono(?=\s|$)", re.IGNORECASE), "pi-mono"),
]
_session_start_locks: dict[str, asyncio.Lock] = {}
_PHASE_LABELS: dict[Phase, str] = {
    Phase.RESEARCH: "research",
    Phase.PLAN: "plan",
    Phase.CLARIFY: "clarification",
    Phase.IMPLEMENT: "implementation",
    Phase.REVIEW: "review",
    Phase.PUBLISH: "publish",
    Phase.DONE: "done",
    Phase.FAILED: "failed",
}


def _build_slack_thread_key(channel: str, thread_ts: str) -> str:
    return f"slack:{channel}:{thread_ts}"


def _normalize_attachments(items: list[dict[str, str]] | None) -> list[dict[str, str]]:
    if not items:
        return []
    normalized: list[dict[str, str]] = []
    for item in items:
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if not name or not url:
            continue
        normalized.append({"name": name, "url": url})
    return normalized


def _attachments_from_event(event: dict) -> list[dict[str, str]]:
    files = event.get("files", [])
    if not isinstance(files, list):
        return []
    parsed: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("title") or "attachment").strip()
        url = str(
            item.get("url_private_download")
            or item.get("url_private")
            or item.get("permalink")
            or ""
        ).strip()
        if not name or not url:
            continue
        parsed.append({"name": name, "url": url})
    return parsed


def _append_attachments(text: str, attachments: list[dict[str, str]] | None) -> str:
    items = _normalize_attachments(attachments)
    if not items:
        return text
    lines = ["Attachments:"]
    for item in items:
        lines.append(f"- {item['name']}: {item['url']}")
    return f"{text}\n\n" + "\n".join(lines)


def _get_start_lock(thread_key: str) -> asyncio.Lock:
    return _session_start_locks.setdefault(thread_key, asyncio.Lock())


async def _mark_event_seen(event_id: str) -> bool:
    now = time.time()
    async with _seen_events_lock:
        expired = [evt for evt, ts in _seen_events.items() if now - ts > _SEEN_EVENT_TTL_SECONDS]
        for evt in expired:
            _seen_events.pop(evt, None)
        if event_id in _seen_events:
            _seen_events.move_to_end(event_id)
            return True
        _seen_events[event_id] = now
        while len(_seen_events) > _MAX_SEEN_EVENTS:
            _seen_events.popitem(last=False)
    return False


def _verify_slack_signature(request: Request, body: bytes, signing_secret: str) -> bool:
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not timestamp or not signature:
        return False

    try:
        ts = int(timestamp)
    except ValueError:
        return False

    if abs(time.time() - ts) > 60 * 5:
        return False

    basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    digest = (
        "v0="
        + hmac.new(
            signing_secret.encode("utf-8"),
            basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(digest, signature)


def _extract_task_text(text: str) -> str:
    return _MENTION_RE.sub("", text).strip()


def _parse_engineer_directives(text: str) -> tuple[str, bool, str | None]:
    """Return (task_text, eng_enabled, model_preference)."""
    cleaned = _extract_task_text(text)
    eng_enabled = bool(_ENG_FLAG_RE.search(cleaned))
    if eng_enabled:
        cleaned = _ENG_FLAG_RE.sub(" ", cleaned)

    model_preference: str | None = None
    kv = _HARNESS_EQ_RE.search(cleaned)
    if kv:
        model_preference = kv.group(1).lower()
        cleaned = _HARNESS_EQ_RE.sub(" ", cleaned)

    for pattern, preference in _MODEL_FLAG_PATTERNS:
        if pattern.search(cleaned):
            model_preference = preference
            cleaned = pattern.sub(" ", cleaned)

    engine_flag = _ENGINE_FLAG_RE.search(cleaned)
    if engine_flag:
        model_preference = engine_flag.group(2).lower()
        cleaned = _ENGINE_FLAG_RE.sub(" ", cleaned)

    model_eq = _MODEL_EQ_RE.search(cleaned)
    if model_eq:
        model_preference = model_eq.group(1)
        cleaned = _MODEL_EQ_RE.sub(" ", cleaned)

    model_flag = _MODEL_FLAG_RE.search(cleaned)
    if model_flag:
        model_preference = model_flag.group(2)
        cleaned = _MODEL_FLAG_RE.sub(" ", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, eng_enabled, model_preference


async def _post_thread_message(
    *,
    token: str,
    channel: str,
    thread_ts: str,
    text: str,
) -> None:
    safe_text = text.strip()
    if len(safe_text) > _MAX_SLACK_MESSAGE_CHARS:
        safe_text = safe_text[: _MAX_SLACK_MESSAGE_CHARS - 18].rstrip() + "\n\n... (truncated)"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "channel": channel,
        "thread_ts": thread_ts,
        "text": safe_text,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json=payload,
        )
    if resp.status_code >= 300:
        raise RuntimeError(f"Slack message failed: {resp.status_code} {resp.text}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack message failed: {data}")


def _route_reply_to_session(thread_key: str, reply_text: str) -> str:
    if not has_active_session(thread_key):
        return "no_active_session"
    session = get_session(thread_key)
    if session is None:
        return "no_active_session"
    session.receive_user_reply(reply_text)
    return "accepted"


def _first_text_block(payload: dict) -> str:
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                return text
    return ""


def _format_event_update(event: dict) -> str | None:
    event_type = str(event.get("type", "")).strip()
    if not event_type:
        return None

    if event_type == "error":
        return f"❌ {event.get('error') or event.get('message') or 'unknown error'}"

    if event_type == "thread.started":
        thread_id = str(event.get("thread_id", "")).strip()
        return f"🧵 Agent thread started: `{thread_id}`" if thread_id else "🧵 Agent thread started"

    if event_type == "system" and event.get("subtype") == "init":
        session_id = str(event.get("session_id", "")).strip()
        return f"✅ Session initialized: `{session_id}`" if session_id else "✅ Session initialized"

    if event_type == "assistant":
        text = _first_text_block(event)
        if text:
            return f"💬 {text[:500]}"
        return None

    if event_type == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = str(item.get("text", "")).strip()
            if text:
                return f"💬 {text[:500]}"
        return None

    if event_type == "result":
        result_text = str(event.get("result", "")).strip()
        if result_text:
            return f"📌 {result_text[:500]}"
        return None

    return None


async def _start_engineer_session(
    *,
    settings: EngineerSettings,
    bot_token: str,
    channel: str,
    thread_ts: str,
    thread_key: str,
    task_text: str,
    model_preference: str | None,
) -> dict[str, str]:
    async with _get_start_lock(thread_key):
        if has_active_session(thread_key):
            existing = get_session(thread_key)
            return {"status": "already_running", "run_id": existing.run_id if existing else ""}

        session = create_session(thread_key, task_text)
        session.model_preference = model_preference

        async def _send(text: str) -> None:
            try:
                await _post_thread_message(
                    token=bot_token,
                    channel=channel,
                    thread_ts=thread_ts,
                    text=text,
                )
            except Exception:
                log.exception("engineer_message_failed", channel=channel)

        async def _run() -> None:
            try:
                if shutil.which("git") is None:
                    await _send("Engineer failed preflight: `git` is not available in the API container.")
                    remove_session(thread_key)
                    return
                if not settings.github_token:
                    await _send(
                        "Engineer failed preflight: `GITHUB_TOKEN` is missing, so PR creation cannot run."
                    )
                    remove_session(thread_key)
                    return
                if not settings.anthropic_api_key:
                    await _send(
                        "Engineer failed preflight: `ANTHROPIC_API_KEY` is missing."
                    )
                    remove_session(thread_key)
                    return
                preference_msg = f" (model preference: {model_preference})" if model_preference else ""
                await _send(f"Engineer started{preference_msg}: `{task_text}`")
                orchestrator = EngineerOrchestrator(
                    settings=settings,
                    model_preference=model_preference,
                )
                last_event_preview = ""
                last_event_at = 0.0

                async def _on_phase(phase: Phase, label: str) -> None:
                    phase_name = _PHASE_LABELS.get(phase, phase.value)
                    suffix = f" — {label}" if label else ""
                    await _send(f"⏱️ Phase: *{phase_name}*{suffix}")

                async def _on_event(event: dict[str, object]) -> None:
                    nonlocal last_event_preview, last_event_at
                    preview = _format_event_update(event)
                    if not preview:
                        return
                    now = time.monotonic()
                    # Avoid flooding Slack with repetitive stream chunks.
                    if preview == last_event_preview and now - last_event_at < 8.0:
                        return
                    if now - last_event_at < 2.0:
                        return
                    last_event_preview = preview
                    last_event_at = now
                    await _send(preview)

                result = await orchestrator.run(
                    session,
                    post_message=_send,
                    on_phase=_on_phase,
                    on_event=_on_event,
                )

                if result.success and result.pr_url:
                    await _send(f"Engineer complete! PR: {result.pr_url}")
                elif result.success:
                    await _send(
                        "Engineer completed but no PR URL was produced. "
                        "The run may have failed during push/PR creation."
                    )
                elif not result.success:
                    await _send(f"Engineer failed: {result.error or 'unknown error'}")
            except Exception:
                log.exception("engineer_task_crashed", thread_key=thread_key)
                await _send("Engineer crashed unexpectedly. Check logs.")
            finally:
                remove_session(thread_key)

        task = asyncio.create_task(_run())
        register_task(thread_key, task)
        return {"status": "started", "run_id": session.run_id}


class EngineerStartRequest(BaseModel):
    thread_key: str
    channel: str
    thread_ts: str
    task: str
    model_preference: str | None = None
    attachments: list[dict[str, str]] | None = None


class EngineerReplyRequest(BaseModel):
    thread_key: str
    reply: str
    attachments: list[dict[str, str]] | None = None


@router.post("/start", dependencies=[Depends(verify_api_key)])
async def start_engineer(payload: EngineerStartRequest) -> JSONResponse:
    settings = engineer_settings
    bot_token = settings.slack_bot_token
    if not bot_token:
        raise HTTPException(status_code=500, detail="Slack bot token is not configured")

    task_text = _append_attachments(payload.task.strip(), payload.attachments)
    if not task_text:
        raise HTTPException(status_code=400, detail="Task must not be empty")

    thread_key = payload.thread_key.strip() or _build_slack_thread_key(payload.channel, payload.thread_ts)
    channel = payload.channel.strip()
    thread_ts = payload.thread_ts.strip()
    if not channel or not thread_ts:
        raise HTTPException(status_code=400, detail="channel and thread_ts are required")

    result = await _start_engineer_session(
        settings=settings,
        bot_token=bot_token,
        channel=channel,
        thread_ts=thread_ts,
        thread_key=thread_key,
        task_text=task_text,
        model_preference=payload.model_preference,
    )
    return JSONResponse(result)


@router.post("/reply", dependencies=[Depends(verify_api_key)])
async def reply_engineer(payload: EngineerReplyRequest) -> JSONResponse:
    thread_key = payload.thread_key.strip()
    reply_text = _append_attachments(payload.reply.strip(), payload.attachments)
    if not thread_key:
        raise HTTPException(status_code=400, detail="thread_key is required")
    if not reply_text:
        return JSONResponse({"status": "ignored_empty"})
    status = _route_reply_to_session(thread_key, reply_text)
    return JSONResponse({"status": status})


@router.post("/events")
async def slack_events(request: Request) -> JSONResponse:
    body = await request.body()
    settings = engineer_settings

    if not settings.slack_signing_secret:
        raise HTTPException(status_code=500, detail="Slack signing secret is not configured")

    if not _verify_slack_signature(request, body, settings.slack_signing_secret):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    payload = json.loads(body.decode("utf-8"))
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True})

    event = payload.get("event", {})
    event_id = str(payload.get("event_id", ""))
    event_type = str(event.get("type", ""))
    if event_type != "app_mention" or not event_id:
        return JSONResponse({"ok": True})

    channel = str(event.get("channel", ""))
    if settings.slack_channel_id and channel != settings.slack_channel_id:
        return JSONResponse({"ok": True})

    user_id = str(event.get("user", ""))
    if not user_id or event.get("bot_id"):
        return JSONResponse({"ok": True})

    if settings.authorized_user_id_set and user_id not in settings.authorized_user_id_set:
        return JSONResponse({"ok": True})

    if await _mark_event_seen(event_id):
        return JSONResponse({"ok": True})

    thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
    task_text, eng_enabled, model_preference = _parse_engineer_directives(str(event.get("text", "")))
    task_text = _append_attachments(task_text, _attachments_from_event(event))
    if not thread_ts or not task_text:
        return JSONResponse({"ok": True})

    bot_token = settings.slack_bot_token
    if not bot_token:
        raise HTTPException(status_code=500, detail="Slack bot token is not configured")

    thread_key = _build_slack_thread_key(channel, thread_ts)

    if _route_reply_to_session(thread_key, task_text) == "accepted":
        return JSONResponse({"ok": True})

    if not eng_enabled:
        return JSONResponse({"ok": True})

    async def _start_from_event() -> None:
        try:
            await _start_engineer_session(
                settings=settings,
                bot_token=bot_token,
                channel=channel,
                thread_ts=thread_ts,
                thread_key=thread_key,
                task_text=task_text,
                model_preference=model_preference,
            )
        except Exception:
            log.exception("engineer_start_from_event_failed", thread_key=thread_key)

    start_task = asyncio.create_task(_start_from_event())
    start_task.add_done_callback(lambda task: task.exception())
    return JSONResponse({"ok": True})
