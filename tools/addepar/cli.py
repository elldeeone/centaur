"""CLI for Addepar API."""

import json

import typer
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()

app = typer.Typer(name="addepar", help="Addepar API — entities, prices, portfolio performance")
console = Console()


def _get_client():
    from .client import AddeparClient

    return AddeparClient()


@app.command()
def entities(
    limit: int = typer.Option(100, "--limit", "-n", help="Max entities to return"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List entities."""
    client = _get_client()
    data = client.list_entities(limit=limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    console.print(f"[bold]Entities ({len(data)})[/bold]\n")
    for e in data:
        attrs = e.get("attributes", {})
        name = attrs.get("original_name", e.get("id", ""))
        console.print(f"  {e['id']:>12}  {name}")


@app.command()
def entity(
    entity_id: int = typer.Argument(..., help="Entity ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get a single entity."""
    client = _get_client()
    data = client.get_entity(entity_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    attrs = data.get("attributes", {})
    console.print(f"[bold]Entity {data.get('id', entity_id)}[/bold]")
    for key, value in attrs.items():
        console.print(f"  {key}: {value}")


@app.command()
def prices(
    entity_id: int = typer.Option(..., "--entity-id", "-e", help="Entity ID"),
    start: str = typer.Option(..., "--start", "-s", help="Start date (YYYY-MM-DD)"),
    end: str = typer.Option(..., "--end", "-e2", help="End date (YYYY-MM-DD)"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get historical prices for an entity."""
    client = _get_client()
    data = client.get_entity_prices(entity_id, start, end)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    console.print(f"[bold]Prices for entity {entity_id} ({len(data)} records)[/bold]\n")
    for p in data:
        attrs = p.get("attributes", {})
        console.print(f"  {attrs.get('date', '')}  {attrs.get('value', '')}")


@app.command()
def performance(
    portfolio: int = typer.Option(0, "--portfolio", "-p", help="Portfolio node ID"),
    start: str = typer.Option(..., "--start", "-s", help="Start date (YYYY-MM-DD)"),
    end: str = typer.Option(..., "--end", "-e", help="End date (YYYY-MM-DD)"),
    columns: str = typer.Option(
        None, "--columns", "-c", help="Comma-separated column keys"
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Run a portfolio performance contribution query."""
    client = _get_client()
    col_list = columns.split(",") if columns else None
    data = client.portfolio_query(start, end, portfolio_id=portfolio, columns=col_list)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    console.print(json.dumps(data, indent=2))


if __name__ == "__main__":
    app()
