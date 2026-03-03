"""CLI for JPMorgan Open Banking API."""

import json

import typer
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()

app = typer.Typer(name="jpm", help="JPMorgan Open Banking CLI — balances and transactions")
console = Console()


@app.command()
def balances(
    date: str = typer.Option(..., "--date", "-d", help="Balance date (YYYY-MM-DD)"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get cash balances for configured accounts."""
    from .client import JPMClient

    client = JPMClient()
    data = client.get_cash_balances(date)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No balances found.[/]")
        raise typer.Exit()

    for account in data:
        console.print(f"[cyan]{account.get('accountId', 'N/A')}[/]")
        for key, value in account.items():
            if key != "accountId":
                console.print(f"  {key}: {value}")
        console.print()


@app.command()
def transactions(
    date: str = typer.Option(..., "--date", "-d", help="Transaction date (YYYY-MM-DD)"),
    limit: int = typer.Option(100, "--limit", "-n", help="Max transactions per page"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get transactions for configured accounts on a given date."""
    from .client import JPMClient

    client = JPMClient()
    data = client.get_transactions(date, limit=limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No transactions found.[/]")
        raise typer.Exit()

    console.print(f"[bold]Transactions on {date}: {len(data)} total[/]\n")
    for txn in data:
        console.print(json.dumps(txn, indent=2))


if __name__ == "__main__":
    app()
