"""Pure functions for parsing harness protocol events.

Extracted from services/sandbox/harness_session.py so the API can interpret
harness NDJSON events without importing sandbox internals.  Every function is
pure — no I/O, no globals, no imports from other api modules.
"""

from __future__ import annotations

import datetime as dt


def _extract_error_message(event: dict) -> str:
    """Extract a human-readable error message from mixed event payload shapes."""
    err = event.get("error")
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str):
            return msg
    msg = event.get("message")
    return msg if isinstance(msg, str) else ""


def is_turn_done(engine: str, event: dict) -> bool:
    """Return True when *event* signals the end of a main-agent turn.

    Subagent events (``parent_tool_use_id`` is set) are ignored — only the
    top-level agent's end-of-turn matters.
    """
    t = event.get("type", "")
    # Wrapper-emitted crash events usually terminate the turn for all engines.
    # Transient amp-wrapper restart notices are non-terminal.
    if t == "error":
        # amp-wrapper emits non-terminal restart notices like
        # "amp exited with code 1, restarting (1/5)". These should not close
        # the turn or clear durable in-flight state.
        error_msg = _extract_error_message(event).lower()
        if "restarting (" in error_msg and "giving up" not in error_msg:
            return False
        return True
    if engine in ("amp", "claude-code"):
        if t == "result":
            return True
        if t == "assistant":
            # Ignore subagent end_turn — only main agent (no parent) counts
            if event.get("parent_tool_use_id") is not None:
                return False
            msg = event.get("message", {})
            content = msg.get("content", [])
            # Amp can emit an assistant event containing only tool_use blocks
            # before the tool_result/final assistant text arrives. Those events
            # must not terminate the durable turn even when stop_reason=end_turn.
            if any(
                block.get("type") == "tool_use"
                for block in content
                if isinstance(block, dict)
            ):
                return False
            return msg.get("stop_reason") == "end_turn"
        return False
    if engine == "codex":
        return t in ("turn.completed", "turn.failed")
    return t == "agent_end"  # pi-mono


def extract_result(engine: str, event: dict) -> str | None:
    """Return the assistant result text from *event*, or ``None``."""
    t = event.get("type", "")
    if t == "turn.done":
        result = event.get("result")
        if isinstance(result, str) and result:
            return result
        if isinstance(result, dict):
            text = result.get("text")
            if isinstance(text, str) and text:
                return text
        return _extract_error_message(event) or None
    if engine in ("amp", "claude-code"):
        if t == "result":
            result = event.get("result")
            if isinstance(result, str) and result:
                return result
            text = event.get("text")
            if isinstance(text, str) and text:
                return text
            return _extract_error_message(event)
        if t == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", [])
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if texts:
                return texts[-1]
        return None
    if engine == "codex":
        if t == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", [])
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if texts:
                return texts[-1]
        if t == "item.completed":
            item = event.get("item", {})
            if item.get("type") in {"agent_message", "agentMessage"}:
                return item.get("text", "")
        return None
    if engine == "pi-mono" and t == "message_end":
        msg = event.get("message", {})
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if content:
                return content[-1].get("text", "")
    return None


def extract_thread_id(engine: str, event: dict) -> str | None:
    """Return the harness thread/session id from *event*, or ``None``."""
    t = event.get("type", "")
    if engine in ("amp", "claude-code"):
        if t == "system" and event.get("subtype") == "init":
            return event.get("session_id") or None
        if t == "assistant":
            return event.get("session_id") or None
    elif engine == "codex":
        if t == "thread.started":
            return event.get("thread_id") or None
    elif engine == "pi-mono" and t == "session":
        return event.get("id") or None
    return None


def build_user_input(
    content_blocks: list[dict],
    *,
    steer: bool = False,
    thread_key: str | None = None,
    trace_id: str | None = None,
    traceparent: str | None = None,
    trace_metadata: dict | None = None,
) -> dict:
    """Build a harness-native user input envelope from content blocks."""
    envelope = {
        "type": "user",
        "message": {
            "role": "user",
            "content": content_blocks,
        },
    }
    if steer:
        envelope["steer"] = True
    if thread_key:
        envelope["thread_key"] = thread_key
    if trace_id:
        envelope["trace_id"] = trace_id
    if traceparent:
        envelope["traceparent"] = traceparent
    if trace_metadata:
        envelope["trace_metadata"] = trace_metadata
    return envelope


def messages_to_content_blocks(messages: list[dict]) -> list[dict]:
    """Flatten messages into a list of content blocks.

    Each message has ``role``, ``parts`` (list of content blocks), and optional
    ``user_id``.  When ``user_id`` is present the first text block in that
    message is prefixed with ``<@user_id>: ``.

    ``attachment_ref`` parts are translated into text download instructions.
    """
    blocks: list[dict] = []
    for message in messages:
        role = message.get("role", "user")
        user_id = message.get("user_id")
        parts = message.get("parts", [])
        metadata_label = _message_metadata_label(message)
        assistant_label = (
            "Previous Centaur response"
            if message.get("history_backfill")
            else "Your previous response"
        )
        if metadata_label:
            assistant_label = f"{assistant_label} | {metadata_label}"
        attributed = False
        for part in parts:
            ptype = part.get("type")
            if role == "assistant":
                if ptype == "text":
                    blocks.append(
                        {
                            "type": "text",
                            "text": f"[{assistant_label}]: {part['text']}",
                        }
                    )
                else:
                    blocks.append(part)
            elif ptype == "attachment_ref":
                att_id = part["id"]
                name = part.get("name", "attachment")
                mime = part.get("mime_type", "")
                blocks.append(
                    {
                        "type": "text",
                        "text": (
                            f"User attached file: {name} ({mime}). "
                            f'Download with: curl -sS -H "Authorization: Bearer '
                            f'$(cat /home/agent/.api_key)" '
                            f'"$CENTAUR_API_URL/agent/attachments/{att_id}/download" -o "{name}"'
                        ),
                    }
                )
            elif user_id and not attributed and ptype == "text":
                prefix = f"[{metadata_label}] " if metadata_label else ""
                blocks.append(
                    {
                        "type": "text",
                        "text": f"{prefix}<@{user_id}>: {part['text']}",
                    }
                )
                attributed = True
            elif metadata_label and not attributed and ptype == "text":
                blocks.append(
                    {
                        "type": "text",
                        "text": f"[{metadata_label}] {part['text']}",
                    }
                )
                attributed = True
            else:
                blocks.append(part)
    return blocks


def _message_metadata_label(message: dict) -> str:
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return ""

    platform = str(metadata.get("platform") or "").strip().lower()
    source = str(metadata.get("source") or "").strip().lower()
    if not platform and not source:
        return ""

    zulip = metadata.get("zulip") if isinstance(metadata.get("zulip"), dict) else {}
    sent_at = _format_sent_at(zulip.get("timestamp") or metadata.get("timestamp"))
    sender = _format_sender(
        zulip.get("sender_full_name") or metadata.get("sender_full_name"),
        zulip.get("sender_email") or metadata.get("sender_email"),
        message.get("user_id") or metadata.get("user_id"),
    )
    message_id = zulip.get("message_id") or metadata.get("message_id")
    topic = zulip.get("topic") or metadata.get("topic")

    items = []
    if platform:
        items.append(f"platform={platform}")
    if sent_at:
        items.append(f"sent_at={sent_at}")
    if sender:
        items.append(f"sender={sender}")
    if message_id is not None:
        items.append(f"message_id={message_id}")
    if topic:
        items.append(f"topic={topic}")
    return "; ".join(items)


def _format_sent_at(value: object) -> str:
    if isinstance(value, (int, float)):
        try:
            return (
                dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OSError, OverflowError, ValueError):
            return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _format_sender(full_name: object, email: object, user_id: object) -> str:
    name = str(full_name or "").strip()
    email_text = str(email or "").strip()
    user_text = str(user_id or "").strip()
    if name and email_text:
        sender = f"{name} <{email_text}>"
    else:
        sender = name or email_text
    if user_text:
        sender = f"{sender} (user_id={user_text})" if sender else f"user_id={user_text}"
    return sender
