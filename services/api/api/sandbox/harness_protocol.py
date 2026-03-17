"""Pure functions for parsing harness protocol events.

Extracted from services/sandbox/harness_session.py so the API can interpret
harness NDJSON events without importing sandbox internals.  Every function is
pure — no I/O, no globals, no imports from other api modules.
"""

from __future__ import annotations


def is_turn_done(engine: str, event: dict) -> bool:
    """Return True when *event* signals the end of a main-agent turn.

    Subagent events (``parent_tool_use_id`` is set) are ignored — only the
    top-level agent's end-of-turn matters.
    """
    t = event.get("type", "")
    # Wrapper-emitted crash events terminate the turn for all engines
    if t == "error":
        return True
    if engine in ("amp", "claude-code"):
        if t == "result":
            return True
        if t == "assistant":
            # Ignore subagent end_turn — only main agent (no parent) counts
            if event.get("parent_tool_use_id") is not None:
                return False
            msg = event.get("message", {})
            return msg.get("stop_reason") == "end_turn"
        return False
    if engine == "codex":
        return t in ("turn.completed", "turn.failed")
    return t == "agent_end"  # pi-mono


def extract_result(engine: str, event: dict) -> str | None:
    """Return the assistant result text from *event*, or ``None``."""
    t = event.get("type", "")
    if engine in ("amp", "claude-code"):
        if t == "result":
            return event.get("result", "")
        if t == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", [])
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if texts:
                return texts[-1]
        return None
    if engine == "codex" and t == "item.completed":
        item = event.get("item", {})
        if item.get("type") == "agent_message":
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
    elif engine == "codex":
        if t == "thread.started":
            return event.get("thread_id") or None
    elif engine == "pi-mono" and t == "session":
        return event.get("id") or None
    return None


def build_user_input(content_blocks: list[dict]) -> dict:
    """Build a harness-native user input envelope from content blocks."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": content_blocks,
        },
    }


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
        attributed = False
        for part in parts:
            ptype = part.get("type")
            if role == "assistant":
                if ptype == "text":
                    blocks.append({
                        "type": "text",
                        "text": f"[Your previous response]: {part['text']}",
                    })
                else:
                    blocks.append(part)
            elif ptype == "attachment_ref":
                att_id = part["id"]
                name = part.get("name", "attachment")
                mime = part.get("mime_type", "")
                blocks.append({
                    "type": "text",
                    "text": (
                        f"User attached file: {name} ({mime}). "
                        f"Download with: curl -sS -H \"Authorization: Bearer "
                        f"$(cat /home/agent/.api_key)\" "
                        f"\"$CENTAUR_API_URL/agent/attachments/{att_id}/download\" -o \"{name}\""
                    ),
                })
            elif user_id and not attributed and ptype == "text":
                blocks.append({
                    "type": "text",
                    "text": f"<@{user_id}>: {part['text']}",
                })
                attributed = True
            else:
                blocks.append(part)
    return blocks
