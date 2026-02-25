"""CLI for conference date monitoring."""

from dotenv import load_dotenv

load_dotenv()

import typer
from rich.console import Console

from ai_v2.cli_tables import Table
from plugins.confmonitor.client import ConfMonitorClient

app = typer.Typer(
    name="confmonitor",
    help="Conference date monitor - checks for new 2026 conference dates",
)
console = Console()


def _client() -> ConfMonitorClient:
    return ConfMonitorClient()


@app.command("check")
def check_dates(
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Don't update sheet or notify"),
    notify: bool = typer.Option(True, "--notify/--no-notify", help="Send Slack notification"),
    channel: str = typer.Option("ai-agent-administration", "--channel", "-c", help="Slack channel"),
):
    """Check all conferences with TBA dates for new 2026 date announcements."""
    console.print("[bold]Checking conference dates...[/]")
    client = _client()

    try:
        tba_conferences, updates = client.check_all_tba()
    except Exception as e:
        console.print(f"[red]Failed to read spreadsheet: {e}[/]")
        raise typer.Exit(1)

    console.print(f"[cyan]Found {len(tba_conferences)} conferences with TBA dates[/]")

    if updates:
        table = Table(title=f"Found {len(updates)} Date Updates")
        table.add_column("Conference", style="cyan")
        table.add_column("Dates Found", style="green")
        table.add_column("Source", style="dim")

        for update in updates:
            table.add_row(update["conference"], update["dates"], update["source"][:40])

        console.print(table)

        if not dry_run:
            console.print("\n[bold]Updating spreadsheet...[/]")
            errors = client.apply_updates(updates)
            for update in updates:
                if not any(update["conference"] in e for e in errors):
                    console.print(f"[green]✓ Updated {update['conference']}[/]")
            for error in errors:
                console.print(f"[red]{error}[/]")

            if notify:
                console.print("\n[bold]Sending Slack notification...[/]")
                try:
                    client.send_slack_notification(updates, channel)
                    console.print("[green]✓ Notification sent[/]")
                except Exception as e:
                    console.print(f"[red]Failed to send notification: {e}[/]")
        else:
            console.print("[yellow]Dry run - no changes made[/]")
    else:
        console.print("[yellow]No new dates found[/]")


@app.command("list-tba")
def list_tba():
    """List all conferences with TBA dates."""
    client = _client()

    try:
        rows = client.get_sheet_data()
    except Exception as e:
        console.print(f"[red]Failed to read spreadsheet: {e}[/]")
        raise typer.Exit(1)

    tba = client.find_tba_conferences(rows)

    table = Table(title="Conferences with TBA Dates")
    table.add_column("Event Name", style="cyan")
    table.add_column("Quarter", style="yellow")
    table.add_column("Category", style="dim")

    for conf in tba:
        row = conf["row"]
        table.add_row(
            row.get("Event Name", ""),
            row.get("Quarter", ""),
            row.get("Ecosystem/Category", ""),
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(tba)} conferences with TBA dates[/]")


@app.command("test-sheet")
def test_sheet():
    """Test reading the conference spreadsheet."""
    client = _client()

    try:
        rows = client.get_sheet_data()
        console.print(f"[green]✓ Successfully read {len(rows)} rows from spreadsheet[/]")

        table = Table(title="First 5 Conferences")
        table.add_column("Event Name", style="cyan")
        table.add_column("Quarter", style="yellow")
        table.add_column("Start Date")
        table.add_column("Location")

        for row in rows[:5]:
            table.add_row(
                row.get("Event Name", "")[:30],
                row.get("Quarter", ""),
                row.get("Start Date", ""),
                row.get("Location", "")[:20],
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]Failed to read spreadsheet: {e}[/]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
