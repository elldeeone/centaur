"""Workflow: weekly MIQ/RIFF tracker for Paradigm Ops and I&R teams.

Runs every Monday at 4:00am PST. Evaluates the previous week (Sun–Thu)
for all members of #miq-operations and #miq-investing-and-research,
writes results to the MIQ tracker Google Sheet, and DMs a summary table
to the team owner.

Status definitions:
  - MIQ    : member posted in channel before midnight PST on that day
  - RIFF   : member posted AND started a thread of 20+ words
             - Ops 2026 rule: thread can be started any time during that same week
             - I&R 2026 rule: thread must be started within 24 hours of the original post
  - No MIQ : no post by midnight PST on that day
  - OOO    : member marked OOO in Google Sheet (manual override for now)

Edge case: Alana Palmedo is on both teams. Her post in
#miq-investing-and-research counts for both Ops and I&R.

Access: operated exclusively by the team owner. The owner's Slack user
ID is hardcoded below so the summary DM cannot be redirected at runtime.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from api.workflow_engine import WorkflowContext

WORKFLOW_NAME = "miq_tracker"


# ── Team rosters (canonical Slack display names) ─────────────────────────────
OPS_MEMBERS = [
    "Alana Palmedo", "David Swain", "Jordan Qualls", "Katie Biber",
    "Justin Slaughter", "Alex Grieve", "Dan McCarthy",
    "Josie Franciose McGuinn", "Veit Moeller", "Chris Kraeuter",
    "Pam Tholen", "Lindsay Slocum", "Ishan Goyal", "Ben Hinshaw",
    "Alex Popescu",
]

IR_MEMBERS = [
    "Alana Palmedo", "Arjun Balaji", "Frankie xyz", "Matt Huang",
    "Alpin Yukseloglu", "Ricardo de Arruda", "Storm Slivkoff",
    "Dan Robinson", "Georgios Konstantopoulos",
]

CHANNELS = {
    "Ops 2026":  "#miq-operations",
    "I&R 2026":  "#miq-investing-and-research",
}

# Active weekdays (Sun=6, Mon=0, Tue=1, Wed=2, Thu=3 in Python weekday())
ACTIVE_WEEKDAYS = {6, 0, 1, 2, 3}  # Sun, Mon, Tue, Wed, Thu

RIFF_MIN_WORDS = 20


@dataclass
class Input:
    # Hardcoded so the DM can't be redirected at runtime
    owner_slack_user_id: str = "REPLACE_WITH_OWNER_USER_ID"  # e.g. "U01234567"
    sheet_id: str = "REPLACE_WITH_GOOGLE_SHEET_ID"
    timezone: str = "America/Los_Angeles"
    run_hour: int = 4     # 4am PST Mondays
    run_minute: int = 0
    max_iterations: int = 0  # 0 = run forever; set to 1 for one-shot test
    dry_run: bool = False    # if True, do not write to sheet or send DM


# ── Core logic ───────────────────────────────────────────────────────────────

def _previous_week_dates(now_pst: dt.datetime) -> list[dt.date]:
    """Return the list of Sun–Thu dates for the week prior to `now_pst`.

    Assumes this function is called on a Monday; returns the five active
    weekdays immediately preceding today.
    """
    today = now_pst.date()
    # Monday -> go back 1 day to reach Sunday, then emit Sun/Mon/Tue/Wed/Thu
    last_sunday = today - dt.timedelta(days=1)
    return [last_sunday + dt.timedelta(days=i) for i in range(5)]


def _word_count(text: str) -> int:
    return len([w for w in text.split() if w.strip()])


def _evaluate_member(
    member: str,
    day: dt.date,
    posts_by_user_day: dict,
    ooo_overrides: dict,
    team_riff_rule: str,  # "same_week" (Ops) or "24h" (I&R)
) -> str:
    """Return one of: MIQ, RIFF, No MIQ, OOO."""
    if ooo_overrides.get((member, day)):
        return "OOO"

    post = posts_by_user_day.get((member, day))
    if not post:
        return "No MIQ"

    # Check thread for RIFF eligibility
    thread_replies = post.get("thread_replies", [])
    own_replies = [r for r in thread_replies if r["user"] == member]

    if team_riff_rule == "24h":
        post_ts = post["ts"]
        cutoff = post_ts + dt.timedelta(hours=24)
        qualifying = [r for r in own_replies if r["ts"] <= cutoff
                      and _word_count(r["text"]) >= RIFF_MIN_WORDS]
    else:  # same_week — any time during Sun–Thu of the same week
        week_start = day - dt.timedelta(days=day.weekday() + 1 if day.weekday() != 6 else 0)
        week_end = week_start + dt.timedelta(days=4)  # Sun..Thu
        qualifying = [r for r in own_replies
                      if week_start <= r["ts"].date() <= week_end
                      and _word_count(r["text"]) >= RIFF_MIN_WORDS]

    return "RIFF" if qualifying else "MIQ"


# ── Handler ──────────────────────────────────────────────────────────────────

async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    """Weekly run loop: evaluate previous week, write sheet, DM owner."""

    iteration = 0
    tz = ZoneInfo(inp.timezone)

    while True:
        iteration += 1
        now = dt.datetime.now(dt.timezone.utc).astimezone(tz)
        week_dates = _previous_week_dates(now)

        ctx.log(f"Evaluating week: {week_dates[0]} .. {week_dates[-1]}")

        # Step 1: Fetch posts + threads from Slack for both channels
        ops_posts = await ctx.step(
            f"fetch_ops_{iteration}",
            lambda: fetch_channel_posts(CHANNELS["Ops 2026"], week_dates),
        )
        ir_posts = await ctx.step(
            f"fetch_ir_{iteration}",
            lambda: fetch_channel_posts(CHANNELS["I&R 2026"], week_dates),
        )

        # Step 2: Read existing OOO overrides from the sheet
        ooo_overrides = await ctx.step(
            f"fetch_ooo_{iteration}",
            lambda: fetch_ooo_overrides(inp.sheet_id, week_dates),
        )

        # Step 3: Evaluate each member for each day
        ops_results = {}
        ir_results = {}

        for day in week_dates:
            if day.weekday() not in ACTIVE_WEEKDAYS:
                continue
            for member in OPS_MEMBERS:
                # Alana: use her I&R post if she's also in I&R
                posts_source = ir_posts if member == "Alana Palmedo" else ops_posts
                ops_results[(member, day)] = _evaluate_member(
                    member, day, posts_source, ooo_overrides, "same_week"
                )
            for member in IR_MEMBERS:
                ir_results[(member, day)] = _evaluate_member(
                    member, day, ir_posts, ooo_overrides, "24h"
                )

        # Step 4: Write results to the Google Sheet
        if not inp.dry_run:
            await ctx.step(
                f"write_sheet_{iteration}",
                lambda: write_results_to_sheet(
                    inp.sheet_id, "Ops 2026", OPS_MEMBERS, week_dates, ops_results
                ),
            )
            await ctx.step(
                f"write_sheet_ir_{iteration}",
                lambda: write_results_to_sheet(
                    inp.sheet_id, "I&R 2026", IR_MEMBERS, week_dates, ir_results
                ),
            )

        # Step 5: Build and send the summary DM
        if not inp.dry_run:
            await ctx.step(
                f"send_dm_{iteration}",
                lambda: send_summary_dm(
                    inp.owner_slack_user_id,
                    week_dates,
                    OPS_MEMBERS, ops_results,
                    IR_MEMBERS, ir_results,
                ),
            )

        # Stop if bounded
        if inp.max_iterations > 0 and iteration >= inp.max_iterations:
            return {
                "status": "done",
                "iterations": iteration,
                "week": [str(d) for d in week_dates],
            }

        # Step 6: Sleep until next Monday at 4am PST
        next_monday = now
        days_until_monday = (0 - now.weekday()) % 7 or 7
        next_monday = now + dt.timedelta(days=days_until_monday)
        next_run = next_monday.replace(
            hour=inp.run_hour, minute=inp.run_minute, second=0, microsecond=0
        )
        if next_run <= now:
            next_run += dt.timedelta(days=7)

        await ctx.sleep(f"wait_{iteration + 1}", next_run - now)


# ── Tool helpers ─────────────────────────────────────────────────────────────
# These are thin wrappers around the Centaur `slack` and `sheets` tools.
# If those tools don't exist yet in the Centaur tool library, build them
# first — see the separate tool scaffolding messages.

def fetch_channel_posts(channel: str, dates: list[dt.date]) -> dict:
    """Return {(user, date): {"ts": datetime, "text": str, "thread_replies": [...]}}
    Uses the `slack` tool's conversations.history + conversations.replies.
    """
    # Implemented via: call slack.history + slack.replies
    # Placeholder — real impl calls the slack tool methods.
    return {}


def fetch_ooo_overrides(sheet_id: str, dates: list[dt.date]) -> dict:
    """Read any OOO entries already in the sheet for the target week.
    Returns {(member, date): True}
    """
    return {}


def write_results_to_sheet(
    sheet_id: str, tab: str, members: list[str],
    dates: list[dt.date], results: dict,
) -> dict:
    """Upsert one row per date. Preserves any pre-existing OOO cells."""
    return {"rows_written": len(dates)}


def send_summary_dm(
    user_id: str, dates: list[dt.date],
    ops_members: list[str], ops_results: dict,
    ir_members: list[str], ir_results: dict,
) -> dict:
    """Build a Slack Block Kit message with colour-coded status badges and DM it."""
    return {"sent_to": user_id}
