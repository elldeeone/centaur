"""Workflow: weekly investment pipeline digest.

Runs every Sunday at 8pm PT. Reads the past week's messages from
#investment-sourcing and the current pipeline Google Doc, then posts
a two-part summary:

1. **New This Week** — new opportunities/people/other from Slack
2. **Follow-Ups From Last Week** — pending next steps from the doc
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from api.runtime_control import ControlPlaneError
from api.workflow_engine import WorkflowContext

WORKFLOW_NAME = "investment_pipeline_weekly"
CRON = "0 20 * * 0"
SLACK_CHANNEL = "investing"
SOURCE_CHANNEL = "investment-sourcing"

_GOOGLE_DOC_ID_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass
class Input:
    document_id: str = "1IjD7m7hCmd_S0fD0fhYi2e7mARKWnr2Ws2fu1S5E4FI"
    document_url: str = ""
    source_channel: str = "investment-sourcing"
    post_channel: str = "investing"
    timezone: str = "America/Los_Angeles"
    lookback_days: int = 7
    max_messages: int = 200
    max_thread_replies: int = 50


def _resolve_document_id(inp: Input) -> str:
    if inp.document_id.strip():
        return inp.document_id.strip()

    if not inp.document_url.strip():
        raise ControlPlaneError(
            "INVALID_WORKFLOW_INPUT",
            "investment_pipeline_weekly requires document_id or document_url",
            422,
        )

    match = _GOOGLE_DOC_ID_RE.search(urlparse(inp.document_url.strip()).path)
    if not match:
        raise ControlPlaneError(
            "INVALID_WORKFLOW_INPUT",
            f"could not extract Google Doc ID from: {inp.document_url}",
            422,
        )
    return match.group(1)


def _normalize_text(value: str) -> str:
    cleaned = _CONTROL_CHAR_RE.sub(" ", value.replace("\r", "\n"))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_doc_text(document: dict[str, Any]) -> str:
    """Extract plain text from a Google Docs API response."""
    parts: list[str] = []

    def _walk(content: list[dict[str, Any]]) -> None:
        for block in content:
            paragraph = block.get("paragraph")
            if paragraph:
                for element in paragraph.get("elements", []):
                    text_run = element.get("textRun")
                    if text_run:
                        parts.append(text_run.get("content", ""))
                continue
            table = block.get("table")
            if table:
                for row in table.get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        _walk(cell.get("content", []))

    # Try tabs first (newer API), fall back to body
    for tab in document.get("tabs", []):
        doc_tab = tab.get("documentTab") or {}
        body = doc_tab.get("body") or {}
        _walk(body.get("content", []))

    if not parts:
        body = document.get("body") or {}
        _walk(body.get("content", []))

    return _normalize_text("".join(parts))


async def _call_tool_raw(ctx: WorkflowContext, tool: str, method: str, args: dict[str, Any]) -> Any:
    """Call a tool and get the raw Python result (bypasses TOON serialization)."""
    from api.app import get_tool_manager

    async def _call() -> Any:
        tm = get_tool_manager()
        return await tm.call_tool_raw(tool, method, args)

    return await ctx.step(f"raw_{tool}_{method}", _call, step_kind="tool_call")


async def _collect_slack_messages(
    ctx: WorkflowContext,
    inp: Input,
    *,
    cutoff_ts: float,
) -> list[dict[str, Any]]:
    """Fetch messages from the source channel and their thread replies, filtered to the lookback window."""
    source = inp.source_channel.strip() or SOURCE_CHANNEL
    raw_messages = await _call_tool_raw(ctx, "slack", "get_channel_history", {
        "channel": source,
        "limit": inp.max_messages,
    })
    if not isinstance(raw_messages, list):
        return []

    messages: list[dict[str, Any]] = []
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        try:
            ts = float(msg.get("timestamp", 0))
        except (ValueError, TypeError):
            continue
        if ts < cutoff_ts:
            continue

        entry: dict[str, Any] = {
            "user": msg.get("user", ""),
            "text": _normalize_text(str(msg.get("text", ""))),
            "timestamp": msg.get("timestamp"),
            "permalink": msg.get("permalink"),
            "reply_count": msg.get("reply_count", 0),
        }

        # Fetch thread replies if any
        channel_id = msg.get("channel_id", "")
        thread_ts = msg.get("thread_ts") or msg.get("timestamp", "")
        if channel_id and thread_ts and int(msg.get("reply_count", 0)) > 0:
            replies = await _call_tool_raw(ctx, "slack", "get_thread_replies", {
                "channel_id": channel_id,
                "thread_ts": str(thread_ts),
                "limit": inp.max_thread_replies,
            })
            if isinstance(replies, list):
                entry["replies"] = [
                    {
                        "user": r.get("user", ""),
                        "text": _normalize_text(str(r.get("text", ""))),
                    }
                    for r in replies
                    if isinstance(r, dict) and str(r.get("text", "")).strip()
                ]

        messages.append(entry)

    return messages


def _section_header(week_label: str) -> str:
    """The section title used in the Google Doc, e.g. 'Pipeline April 12, 2026'."""
    return f"Pipeline {week_label}"


def _build_prompt(
    *,
    slack_messages: list[dict[str, Any]],
    doc_text: str,
    week_label: str,
) -> str:
    serialized_slack = json.dumps(slack_messages, indent=2)

    return (
        f"Generate the weekly investment pipeline digest for the week of {week_label}.\n\n"
        "You have two data sources:\n\n"
        "## Source 1: New Slack messages from #investment-sourcing (past 7 days)\n"
        f"```json\n{serialized_slack}\n```\n\n"
        "## Source 2: Current pipeline document\n"
        f"```text\n{doc_text}\n```\n\n"
        "---\n\n"
        "Produce a plain-text digest (NO markdown formatting — no *, **, #, or ``` — "
        "this will be inserted into a Google Doc) with exactly two sections:\n\n"
        "Section 1: New This Week\n"
        "New opportunities and updates from Slack that are NOT already in the pipeline doc.\n"
        "CRITICAL: Each entry must be ONE LINE only. Format:\n"
        "[INITIALS] Company Name — one sentence max covering what they do, deal terms, and next step.\n"
        "Example: [FS] Acme Robotics — autonomous warehouse robots; raising $15M seed; meeting founder Thursday.\n"
        "Group into subsections: Opportunities, People, Other (omit empty subsections).\n"
        "If a Slack message updates an existing pipeline item, include it with the update.\n\n"
        "Section 2: Follow-Ups From Last Week\n"
        "Extract every item from the pipeline doc that has a next step, action item, or open question.\n"
        "CRITICAL: Each entry must be ONE LINE only. Format:\n"
        "[INITIALS] Company/Person — the specific pending action.\n"
        "Example: [MH] Morpho — catch up with Paul this week on the $50M opportunity.\n\n"
        "Rules:\n"
        "- Plain text only. No markdown. No intro or closing.\n"
        "- ONE LINE PER ENTRY. Do not expand into multiple lines or bullet points.\n"
        "- Be concise. Compress all context into a single sentence.\n"
        "- Deduplicate: if the same company appears in Slack and the doc, mention it once in "
        "'New This Week' with the latest context.\n"
        "- Omit bot messages, reactions-only messages, and obvious noise.\n"
    )


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    document_id = _resolve_document_id(inp)
    channel = inp.post_channel.strip() or SLACK_CHANNEL
    tz = ZoneInfo(inp.timezone)
    now_local = dt.datetime.now(dt.timezone.utc).astimezone(tz)
    cutoff = now_local - dt.timedelta(days=inp.lookback_days)
    cutoff_ts = cutoff.timestamp()
    week_label = now_local.strftime("%B %d, %Y")
    header = _section_header(week_label)

    # Collect data from both sources
    slack_messages = await _collect_slack_messages(ctx, inp, cutoff_ts=cutoff_ts)

    document = await ctx.tools.gsuite.docs_get(
        document_id=document_id,
        include_tabs=True,
    )
    doc_text = _extract_doc_text(document)

    # Generate the digest
    prompt = _build_prompt(
        slack_messages=slack_messages,
        doc_text=doc_text,
        week_label=week_label,
    )
    result = await ctx.agent_turn(prompt)
    text = result.get("result_text", "")

    if text:
        # Write to the Google Doc — insert new section at top or append to existing
        section_exists = header in doc_text
        if section_exists:
            # We already have doc_text from earlier — find the header position
            header_pos = doc_text.find(header)
            if header_pos >= 0:
                # Google Docs index is 1-based and offset by 1 for the body start
                # Insert after the header line (header + newline)
                insert_at = header_pos + len(header) + 2  # +1 for body offset, +1 for newline
                await ctx.tools.gsuite.docs_insert(
                    document_id=document_id,
                    text=f"\n{text}\n",
                    index=insert_at,
                )
        else:
            # Insert new section at the top of the doc
            await ctx.tools.gsuite.docs_insert(
                document_id=document_id,
                text=f"{header}\n\n{text}\n\n",
                index=1,
            )

        # Post to Slack with provenance and link to the doc
        doc_url = f"https://docs.google.com/document/d/{document_id}"
        source = inp.source_channel.strip() or SOURCE_CHANNEL
        slack_text = (
            f"*Weekly Pipeline Summary* (from #investment-sourcing)\n\n"
            f"{text}\n\n"
            f"_Added to <{doc_url}|pipeline doc>._"
        )
        await ctx.post_to_slack(channel, slack_text)

    return result
