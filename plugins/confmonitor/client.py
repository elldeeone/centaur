"""Conference monitor client."""

import json
import re
import subprocess
from datetime import datetime
from typing import Optional

import httpx

SPREADSHEET_ID = "1AgNeNaIVgWl7jIovJsvW-F1zIz150e-nCr4VCE56odE"

CONFERENCE_URLS = {
    "ETHRiyadh": "https://ethriyadh.io",
    "Prediction Markets Conference": "https://www.predictionmarketsconference.com",
    "Berlin Blockchain Week": "https://blockchainweek.berlin",
    "Solana Crossroads": "https://crossroads.solana.com",
    "Solana APEX": "https://apex.solana.com",
    "ETHTaipei": "https://ethtaipei.org",
    "Columbia Crypto Economics": "https://economics.engineering.columbia.edu/blockchain",
    "Tokenized Live": "https://tokenized.live",
    "Ondo Summit": "https://summit.ondo.finance",
    "Sequoia AI Ascent": "https://www.sequoiacap.com/ai-ascent",
    "Stripe Sessions": "https://stripe.com/sessions",
    "Stripe Tour": "https://stripe.com/tour",
    "Fintech NerdCon 2026": "https://fintechnerdcon.com",
    "Hill and Valley Forum": "https://hillandvalleyforum.com",
    "Blockchain Futurist Conference Florida": "https://futuristconference.com",
    "Avalanche Summit": "https://summit.avax.network",
    "Permissionless": "https://blockworks.co/event/permissionless",
    "Korea Blockchain Week": "https://koreablockchainweek.com",
    "NeurIPS": "https://nips.cc",
    "OpenAI Dev Day": "https://openai.com/devday",
    "Manifest": "https://manifest.is",
    "USC Blockchain Conference": "https://blockchain.usc.edu",
}


class ConfMonitorClient:
    """Client for conference date monitoring."""

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def _run_gsuite_cmd(self, args: list[str]) -> str:
        """Run gsuite CLI command and return output."""
        result = subprocess.run(
            ["gsuite", "-a", "svc_ai@paradigm.xyz"] + args,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gsuite command failed: {result.stderr}")
        return result.stdout

    def _run_slack_cmd(self, args: list[str]) -> str:
        """Run slack CLI command and return output."""
        result = subprocess.run(["slack"] + args, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"slack command failed: {result.stderr}")
        return result.stdout

    def get_sheet_data(self) -> list[dict]:
        """Read conference data from the Google Sheet."""
        output = self._run_gsuite_cmd(["sheets", "read", SPREADSHEET_ID, "--json"])
        return json.loads(output)

    def search_conference_dates(self, conference_name: str) -> Optional[dict]:
        """Search the web for conference 2026 dates.

        Returns dict with raw_match and source if found.
        """
        search_query = f"{conference_name} 2026 dates location"

        try:
            response = httpx.get(
                "https://html.duckduckgo.com/html/",
                params={"q": search_query},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            text = response.text.lower()

            date_patterns = [
                r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?,?\s*2026",
                r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+2026",
                r"2026[-/](\d{2})[-/](\d{2})",
            ]

            for pattern in date_patterns:
                match = re.search(pattern, text)
                if match:
                    return {"raw_match": match.group(0), "source": "web_search"}

            return None
        except Exception:
            return None

    def check_conference_website(self, conference_name: str, url: str) -> Optional[dict]:
        """Check a conference website directly for 2026 dates."""
        try:
            response = httpx.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=self.timeout,
                follow_redirects=True,
            )
            text = response.text

            date_patterns = [
                r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?,?\s*2026",
                r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+2026",
            ]

            for pattern in date_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    return {"raw_match": match.group(0), "source": url}

            return None
        except Exception:
            return None

    def update_sheet_cell(self, row_index: int, column: str, value: str) -> None:
        """Update a specific cell in the sheet."""
        col_map = {
            "Quarter": "C",
            "Start Date": "D",
            "End Date": "E",
            "Location": "F",
            "Notes": "H",
        }
        col_letter = col_map.get(column, "H")
        cell_range = f"{col_letter}{row_index + 2}"

        self._run_gsuite_cmd(
            ["sheets", "update", SPREADSHEET_ID, cell_range, json.dumps([[value]])]
        )

    def send_slack_notification(
        self, updates: list[dict], channel: str = "ai-agent-administration"
    ) -> None:
        """Send Slack notification about found conference dates."""
        if not updates:
            return

        message_lines = ["*🗓️ Conference Date Updates Found*\n"]
        for update in updates:
            message_lines.append(
                f"• *{update['conference']}*: {update['dates']} (source: {update['source']})"
            )

        message = "\n".join(message_lines)
        message += "\n\n<@U03RE7C21RL>"

        self._run_slack_cmd(["send", f"#{channel}", message])

    def find_tba_conferences(self, rows: list[dict]) -> list[dict]:
        """Find conferences with TBA dates from sheet rows."""
        tba = []
        for i, row in enumerate(rows):
            event_name = row.get("Event Name", "")
            quarter = row.get("Quarter", "")
            start_date = row.get("Start Date", "")

            if "TBA" in quarter.upper() or (not start_date and event_name):
                tba.append({"index": i, "name": event_name, "row": row})
        return tba

    def check_dates(self, conference_name: str) -> Optional[dict]:
        """Check a single conference for date announcements.

        Tries the known website first, then falls back to web search.
        """
        url = CONFERENCE_URLS.get(conference_name)
        if url:
            result = self.check_conference_website(conference_name, url)
            if result:
                return result

        return self.search_conference_dates(conference_name)

    def check_all_tba(self) -> tuple[list[dict], list[dict]]:
        """Check all TBA conferences for new dates.

        Returns (tba_conferences, updates).
        """
        rows = self.get_sheet_data()
        tba_conferences = self.find_tba_conferences(rows)

        updates = []
        for conf in tba_conferences:
            name = conf["name"]
            if not name:
                continue

            result = self.check_dates(name)
            if result:
                updates.append(
                    {
                        "conference": name,
                        "dates": result["raw_match"],
                        "source": result["source"],
                        "row_index": conf["index"],
                    }
                )

        return tba_conferences, updates

    def apply_updates(self, updates: list[dict]) -> list[str]:
        """Apply date updates to the spreadsheet. Returns list of error messages."""
        errors = []
        for update in updates:
            try:
                self.update_sheet_cell(
                    update["row_index"],
                    "Notes",
                    f"Dates found: {update['dates']} "
                    f"(auto-detected {datetime.now().strftime('%Y-%m-%d')})",
                )
            except Exception as e:
                errors.append(f"Failed to update {update['conference']}: {e}")
        return errors


def _client() -> ConfMonitorClient:
    return ConfMonitorClient()
