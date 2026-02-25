"""CLI for social feed monitoring and career signal detection."""

from dotenv import load_dotenv

load_dotenv()

from datetime import datetime

import typer
from rich.console import Console

from ai_v2.cli_tables import Table

from .client import _client
from .digest import format_signal

app = typer.Typer(name="social-monitor", help="Social feed monitor for career signals")
console = Console()


@app.command("add-person")
def cmd_add_person(
    name: str = typer.Argument(..., help="Person's full name"),
    twitter: str | None = typer.Option(None, "--twitter", "-t", help="Twitter handle"),
    linkedin: str | None = typer.Option(None, "--linkedin", "-l", help="LinkedIn URL"),
    company: str | None = typer.Option(None, "--company", "-c", help="Current company"),
    role: str | None = typer.Option(None, "--role", "-r", help="Current role"),
    category: str | None = typer.Option(None, "--category", help="Category to assign"),
) -> None:
    """Add a person to track."""
    client = _client()
    person_id = client.add_person(
        name=name, twitter=twitter, linkedin=linkedin, company=company, role=role, category=category
    )
    console.print(f"[green]Added person: {name} (id={person_id})[/green]")
    if category:
        console.print(f"  Assigned to category: {category}")


@app.command("add-category")
def cmd_add_category(
    name: str = typer.Argument(..., help="Category name"),
    description: str | None = typer.Option(None, "--description", "-d", help="Description"),
) -> None:
    """Add a tracking category."""
    client = _client()
    cat_id = client.add_category(name, description)
    console.print(f"[green]Added category: {name} (id={cat_id})[/green]")


@app.command("import")
def cmd_import(
    csv_path: str = typer.Argument(..., help="Path to CSV file"),
    category_name: str = typer.Argument(..., help="Category to assign imported people to"),
) -> None:
    """Import people from CSV."""
    client = _client()
    count = client.import_people(csv_path, category_name)
    console.print(f"[green]Imported {count} people into category '{category_name}'.[/green]")


@app.command("list-people")
def cmd_list_people(
    category: str | None = typer.Option(None, "--category", "-c", help="Filter by category"),
) -> None:
    """List tracked people."""
    client = _client()
    people = client.list_people(category=category)

    if not people:
        console.print("[yellow]No people found.[/yellow]")
        return

    table = Table(title="Tracked People")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Twitter")
    table.add_column("Company")
    table.add_column("Role")

    for p in people:
        table.add_row(
            str(p["id"]),
            p["name"],
            f"@{p['twitter_handle']}" if p.get("twitter_handle") else "",
            p.get("company") or "",
            p.get("role") or "",
        )

    console.print(table)


@app.command("list-categories")
def cmd_list_categories() -> None:
    """List categories."""
    client = _client()
    categories = client.list_categories()

    if not categories:
        console.print("[yellow]No categories found.[/yellow]")
        return

    table = Table(title="Categories")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Description")

    for c in categories:
        table.add_row(str(c["id"]), c["name"], c.get("description") or "")

    console.print(table)


@app.command("scan")
def cmd_scan(
    limit: int = typer.Option(20, "--limit", "-n", help="Posts per person"),
) -> None:
    """Run Twitter scan for all tracked people."""
    client = _client()
    client.scan(limit_per_person=limit)


@app.command("classify")
def cmd_classify() -> None:
    """Run signal classification on unprocessed posts."""
    client = _client()
    client.classify()


@app.command("digest")
def cmd_digest(
    channel: str | None = typer.Option(None, "--channel", "-c", help="Slack channel"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print digest without sending"),
) -> None:
    """Send daily digest."""
    client = _client()
    if dry_run:
        signals = client.get_unnotified_signals(min_confidence=0.5)
        if not signals:
            console.print("[yellow]No signals to report.[/yellow]")
        else:
            today = datetime.now().strftime("%B %d, %Y")
            console.print("\n[bold]Social Feed Monitor — Daily Digest[/bold]")
            console.print(f"[dim]{today}[/dim]\n")
            console.print(f"{len(signals)} career signal(s) detected:\n")
            for signal in signals:
                console.print(format_signal(signal))
                console.print()
    else:
        client.send_digest(channel=channel)


@app.command("run")
def cmd_run(
    channel: str | None = typer.Option(None, "--channel", "-c", help="Slack channel"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print digest without sending"),
    limit: int = typer.Option(20, "--limit", "-n", help="Posts per person"),
) -> None:
    """Full pipeline: scan + classify + digest."""
    console.print("[bold]Step 1: Scanning Twitter feeds...[/bold]\n")
    client = _client()
    client.scan(limit_per_person=limit)

    console.print("\n[bold]Step 2: Classifying posts...[/bold]\n")
    client.classify()

    console.print("\n[bold]Step 3: Sending digest...[/bold]\n")
    if dry_run:
        signals = client.get_unnotified_signals(min_confidence=0.5)
        if not signals:
            console.print("[yellow]No signals to report.[/yellow]")
        else:
            today = datetime.now().strftime("%B %d, %Y")
            console.print("\n[bold]Social Feed Monitor — Daily Digest[/bold]")
            console.print(f"[dim]{today}[/dim]\n")
            console.print(f"{len(signals)} career signal(s) detected:\n")
            for signal in signals:
                console.print(format_signal(signal))
                console.print()
    else:
        client.send_digest(channel=channel)


@app.command("stats")
def cmd_stats() -> None:
    """Show database statistics."""
    client = _client()
    s = client.stats()

    table = Table(title="Social Monitor Stats")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")

    table.add_row("People tracked", str(s["people"]))
    table.add_row("Posts fetched", str(s["posts"]))
    table.add_row("Signals detected", str(s["signals"]))

    console.print(table)

    if s["categories"]:
        cat_table = Table(title="By Category")
        cat_table.add_column("Category", style="bold")
        cat_table.add_column("People", justify="right")

        for name, count in s["categories"].items():
            cat_table.add_row(name, str(count))

        console.print(cat_table)


@app.command("signals")
def cmd_signals(
    limit: int = typer.Option(20, "--limit", "-n", help="Max signals to show"),
    min_confidence: float = typer.Option(0.5, "--min-confidence", help="Minimum confidence"),
) -> None:
    """List recent signals."""
    client = _client()
    rows = client.get_signals(limit=limit, min_confidence=min_confidence)

    if not rows:
        console.print("[yellow]No signals found.[/yellow]")
        return

    table = Table(title="Recent Signals")
    table.add_column("ID", style="dim")
    table.add_column("Person", style="bold")
    table.add_column("Signal", style="bold red")
    table.add_column("Confidence", justify="right")
    table.add_column("Reasoning")
    table.add_column("Notified")

    for row in rows:
        table.add_row(
            str(row["id"]),
            row["person_name"],
            row["signal_type"],
            f"{row['confidence']:.0%}",
            (row.get("reasoning") or "")[:60],
            "✓" if row["notified"] else "",
        )

    console.print(table)
