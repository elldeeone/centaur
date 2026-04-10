"""Workflow: daily Paradigm Pulse digest.

Posts to #paradigm-pulse every morning at 7:45am PT.
"""

WORKFLOW_NAME = "paradigm_pulse_daily"
CRON = "45 7 * * *"
SLACK_CHANNEL = "paradigm-pulse"

PROMPT = (
    "Generate today's Paradigm Pulse digest for Paradigm I&R and "
    "Marketing. Use Centaur tools to gather fresh signals across "
    "Paradigm mentions, Paradigm team activity, portfolio company "
    "momentum, relevant market/news signals, and notable "
    "influential-circle content.\n\n"
    "Output concise Slack-ready markdown with these sections when "
    "there is signal:\n"
    "- News\n"
    "- Trending\n"
    "- Paradigm & Team\n"
    "- Holdings\n"
    "- Influential Circles\n\n"
    "Avoid low-signal filler. Reuse the existing thread context to "
    "avoid repeating items that were already posted recently unless "
    "they changed materially. Prefer links inline and keep the "
    "final answer readable in Slack."
)
