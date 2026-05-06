"""Workflow: batch-generate LP briefing memos for a date or rolling window."""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from api.runtime_control import ControlPlaneError
from api.workflow_engine import Delivery, WorkflowContext

WORKFLOW_NAME = "lp_meeting_brief_batch"
IR_CALENDAR_ID = (
    "c_f7f190412d9e371ce1b93530a918db7f89499c640f5133c5cdac01cbfe244dd0"
    "@group.calendar.google.com"
)
INTERNAL_DOMAINS = {"paradigm.xyz"}
LP_KEYWORDS = (
    "lp",
    "limited partner",
    "allocator",
    "allocators",
    "endowment",
    "pension",
    "retirement system",
    "retirement fund",
    "superannuation",
    "sovereign wealth",
    "sovereign fund",
    "family office",
    "investment office",
    "institutional investor",
    "trust",
)
NON_LP_KEYWORDS = (
    "all hands",
    "standup",
    "candidate",
    "interview",
    "performance review",
    "portfolio company",
    "board meeting",
    "weekly sync",
)
DOC_URL_RE = re.compile(r"https://docs\.google\.com/document/d/[^\s)>]+")


@dataclass
class Input:
    target_date: str = ""
    lookahead_hours: int = 24
    timezone: str = "America/Los_Angeles"
    calendar_ids: list[str] = field(default_factory=lambda: [IR_CALENDAR_ID])
    max_results_per_calendar: int = 50
    create_docs: bool = True
    create_bundle_doc: bool = True
    delivery: Delivery = field(default_factory=Delivery)
    metadata: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None


def _parse_iso_datetime(value: str) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _window_bounds(inp: Input, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    tz = ZoneInfo(inp.timezone)
    if inp.target_date.strip():
        day = dt.date.fromisoformat(inp.target_date.strip())
        start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
        end = start + dt.timedelta(days=1)
        return start, end

    lookahead = max(inp.lookahead_hours, 1)
    start = now.astimezone(tz)
    end = start + dt.timedelta(hours=lookahead)
    return start, end


def _event_external_attendees(event: dict[str, Any]) -> list[str]:
    attendees = event.get("attendees") or []
    externals: list[str] = []
    for attendee in attendees:
        email = str(attendee or "").strip()
        if "@" not in email:
            continue
        domain = email.rsplit("@", 1)[-1].lower()
        if domain in INTERNAL_DOMAINS:
            continue
        externals.append(email)
    return externals


def _looks_like_lp_meeting(event: dict[str, Any], calendar_id: str) -> bool:
    if not bool(event.get("has_visibility", True)):
        return False

    summary = str(event.get("summary") or "").strip().lower()
    if summary in {"", "[private event]", "[busy - details not visible]"}:
        return False

    external_attendees = _event_external_attendees(event)
    if not external_attendees:
        return False

    if calendar_id == IR_CALENDAR_ID:
        return True

    haystack = " ".join(
        [
            summary,
            str(event.get("description") or "").lower(),
            " ".join(external_attendees).lower(),
        ]
    )
    if any(keyword in haystack for keyword in NON_LP_KEYWORDS):
        return False
    return any(keyword in haystack for keyword in LP_KEYWORDS)


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for event in events:
        key = str(event.get("id") or "").strip() or (
            f"{event.get('summary', '')}::{event.get('start', '')}::{event.get('calendar_id', '')}"
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def _event_sort_key(event: dict[str, Any]) -> tuple[str, str]:
    return (str(event.get("start") or ""), str(event.get("summary") or ""))


def _display_time(value: str, timezone: str) -> str:
    parsed = _parse_iso_datetime(value)
    if parsed is None:
        return value
    localized = parsed.astimezone(ZoneInfo(timezone))
    return localized.strftime("%Y-%m-%d %I:%M %p %Z")


def _sanitize_title(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]", "", cleaned)
    return cleaned[:100] or "LP Meeting"


def _brief_prompt(event: dict[str, Any], timezone: str) -> str:
    attendees = event.get("attendees") or []
    attendee_lines = (
        "\n".join(f"- {attendee}" for attendee in attendees)
        or "- [no attendees listed]"
    )
    return (
        "Prepare an LP briefing memo for this meeting.\n\n"
        "This is an explicit LP briefing request. Use the `LP-meeting-prep` skill if it is "
        "available in this deployment. If that skill is unavailable, follow this fallback "
        "checklist instead:\n"
        "1. Verify any Slack, Gmail, calendar, or Attio claim with successful tool calls before "
        "you write it.\n"
        "2. If a system cannot be checked, label it clearly as unavailable or not checked.\n"
        "3. Never claim that no extra context surfaced from a system unless that lookup succeeded.\n"
        "4. Use verified public organization facts even when internal systems are unavailable.\n"
        "5. Return only the finished memo.\n\n"
        f"Meeting title: {event.get('summary', '')}\n"
        f"Start: {_display_time(str(event.get('start') or ''), timezone)}\n"
        f"End: {_display_time(str(event.get('end') or ''), timezone)}\n"
        f"Location: {event.get('location', '') or '[not listed]'}\n"
        f"Calendar link: {event.get('html_link', '') or '[not listed]'}\n"
        f"Attendees:\n{attendee_lines}\n"
    )


def _agent_result_text(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value or "").strip()
    for key in ("result_text", "memo_text", "text"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    for key in ("output_json", "output", "execution"):
        nested = value.get(key)
        if isinstance(nested, dict):
            text = _agent_result_text(nested)
            if text:
                return text
    return ""


def _bundle_markdown(
    events: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    *,
    timezone: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> str:
    lines = [
        "LP Meeting Brief Batch",
        f"Window: {window_start.strftime('%Y-%m-%d %I:%M %p %Z')} -> {window_end.strftime('%Y-%m-%d %I:%M %p %Z')}",
        "",
    ]
    for event, doc in zip(events, docs, strict=False):
        lines.append(
            f"- {_display_time(str(event.get('start') or ''), timezone)} — {event.get('summary', '')}"
        )
        if event.get("html_link"):
            lines.append(f"  Calendar: {event['html_link']}")
        if doc.get("url"):
            lines.append(f"  Brief doc: {doc['url']}")
        for url in doc.get("extra_doc_urls", []):
            lines.append(f"  Referenced doc: {url}")
        lines.append("")
    return "\n".join(lines).strip()


async def _call_tool_step(
    ctx: WorkflowContext,
    *,
    step_name: str,
    tool: str,
    method: str,
    args: dict[str, Any],
) -> Any:
    from api.app import get_tool_manager

    async def _call() -> Any:
        tm = get_tool_manager()
        raw = await tm.call_tool(tool, method, args)
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return {"raw": raw}

    return await ctx.step(step_name, _call, step_kind="tool_call")


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    window_start, window_end = _window_bounds(inp, now)

    raw_events: list[dict[str, Any]] = []
    for calendar_index, calendar_id in enumerate(inp.calendar_ids, start=1):
        result = await _call_tool_step(
            ctx,
            step_name=f"calendar_events_{calendar_index}",
            tool="gsuite",
            method="calendar_events",
            args={
                "calendar_id": calendar_id,
                "time_min": window_start.isoformat(),
                "time_max": window_end.isoformat(),
                "max_results": max(inp.max_results_per_calendar, 1),
            },
        )
        for event in result if isinstance(result, list) else []:
            if not isinstance(event, dict):
                continue
            candidate = {**event, "calendar_id": calendar_id}
            if _looks_like_lp_meeting(candidate, calendar_id):
                raw_events.append(candidate)

    events = sorted(_dedupe_events(raw_events), key=_event_sort_key)
    if not events:
        return {
            "status": "no_meetings",
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "calendar_ids": inp.calendar_ids,
            "meetings": [],
        }

    docs: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        memo = await ctx.run_agent(
            f"brief_{index}",
            text=_brief_prompt(event, inp.timezone),
            user_id=inp.user_id,
            metadata={
                **inp.metadata,
                "source": WORKFLOW_NAME,
                "meeting_index": index,
                "meeting_summary": event.get("summary", ""),
            },
        )
        child_status = str(memo.get("status") or "").strip().lower() if isinstance(memo, dict) else ""
        if child_status and child_status != "completed":
            raise ControlPlaneError(
                "LP_BRIEF_CHILD_FAILED",
                f"LP brief child workflow failed for meeting {index}: {child_status}",
                502,
            )
        memo_text = _agent_result_text(memo)
        if not memo_text:
            raise ControlPlaneError(
                "LP_BRIEF_EMPTY",
                f"LP brief child workflow returned no memo text for meeting {index}",
                502,
            )
        doc_entry = {
            "title": "",
            "url": "",
            "document_id": "",
            "extra_doc_urls": DOC_URL_RE.findall(memo_text),
            "memo_text": memo_text,
        }
        if inp.create_docs and memo_text:
            start_label = _display_time(
                str(event.get("start") or ""), inp.timezone
            ).split(" ")[0]
            title = f"LP Brief - {_sanitize_title(str(event.get('summary') or ''))} - {start_label}"
            created = await _call_tool_step(
                ctx,
                step_name=f"create_doc_{index}",
                tool="gsuite",
                method="docs_create",
                args={"title": title, "content": memo_text},
            )
            if isinstance(created, dict):
                doc_entry.update(
                    {
                        "title": str(created.get("title") or title),
                        "url": str(created.get("url") or ""),
                        "document_id": str(created.get("document_id") or ""),
                    }
                )
        docs.append(doc_entry)

    bundle_text = _bundle_markdown(
        events,
        docs,
        timezone=inp.timezone,
        window_start=window_start,
        window_end=window_end,
    )
    bundle_doc: dict[str, Any] | None = None
    if inp.create_bundle_doc and inp.create_docs and bundle_text:
        bundle_doc = await _call_tool_step(
            ctx,
            step_name="create_bundle_doc",
            tool="gsuite",
            method="docs_create",
            args={
                "title": f"LP Brief Batch - {window_start.strftime('%Y-%m-%d')}",
                "content": bundle_text,
            },
        )

    return {
        "status": "completed",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "calendar_ids": inp.calendar_ids,
        "meetings": [
            {
                "summary": event.get("summary", ""),
                "start": event.get("start", ""),
                "end": event.get("end", ""),
                "html_link": event.get("html_link", ""),
                "attendees": event.get("attendees", []),
                "calendar_id": event.get("calendar_id", ""),
                "doc_url": doc.get("url", ""),
            }
            for event, doc in zip(events, docs, strict=False)
        ],
        "bundle_text": bundle_text,
        "bundle_doc": bundle_doc or {},
    }
