"""Workflow: ETH morning brief.

Searches for overnight ETH news and posts a summary to #trading.
Runs at 8am PT on weekdays. Set ETH_MORNING_BRIEF_SLACK_CHANNEL to override.
"""

from __future__ import annotations

from typing import Any

from api.workflow_engine import WorkflowContext

WORKFLOW_NAME = "eth_morning_brief"
CRON = "0 8 * * 1-5"
SLACK_CHANNEL = "trading"


async def handler(inp: dict[str, Any], ctx: WorkflowContext) -> dict[str, Any]:
    channel = inp.get("slack_channel") or SLACK_CHANNEL

    news = await ctx.tools.websearch.search(
        query="Ethereum ETH news last 24 hours",
        max_age_hours=24,
        num_results=10,
    )
    summary = news.get("synthesis", "") or news.get("answer", "")
    if not summary:
        summary = "No significant ETH news in the last 24 hours."

    result = await ctx.agent_turn(
        f"Write a concise morning brief for the trading team based on "
        f"this overnight ETH news. Lead with what matters, skip filler.\n\n"
        f"{summary}"
    )
    text = result.get("result_text", "")
    if text:
        await ctx.post_to_slack(channel, text)
    return result
