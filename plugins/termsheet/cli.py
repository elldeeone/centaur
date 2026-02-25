"""CLI for term sheet generation and deal tracking."""

from dotenv import load_dotenv

load_dotenv()

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from ai_v2.cli_tables import Table

from .client import TermsheetClient
from .models import BoardRights, DealStatus, InstrumentType, TokenRights

app = typer.Typer(help="Term sheet generation and deal tracking for Paradigm")
console = Console()
client = TermsheetClient()


def _format_money(amount: float) -> str:
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f}B"
    elif amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    elif amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    else:
        return f"${amount:,.0f}"


def _status_emoji(status: DealStatus) -> str:
    return {
        DealStatus.DRAFT: "📝",
        DealStatus.PENDING_APPROVAL: "⏳",
        DealStatus.APPROVED: "✅",
        DealStatus.SENT: "📤",
    }.get(status, "❓")


@app.command("create")
def create_term_sheet(
    company: str = typer.Argument(..., help="Company name"),
    amount: float = typer.Option(..., "--amount", "-a", help="Investment amount in USD"),
    instrument: str = typer.Option(
        "priced", "--instrument", "-i", help="Instrument type: safe, priced, convertible_note"
    ),
    valuation_cap: Optional[float] = typer.Option(
        None, "--cap", help="Valuation cap (for SAFE/convertible)"
    ),
    discount: Optional[float] = typer.Option(
        None, "--discount", help="Discount percentage (for SAFE/convertible)"
    ),
    pre_money: Optional[float] = typer.Option(
        None, "--pre-money", help="Pre-money valuation (for priced rounds)"
    ),
    post_money: Optional[float] = typer.Option(
        None, "--post-money", help="Post-money valuation (for priced rounds)"
    ),
    series: Optional[str] = typer.Option(
        None, "--series", "-s", help="Series name (A, B, C, Seed)"
    ),
    option_pool: float = typer.Option(10.0, "--option-pool", help="Option pool percentage"),
    option_timing: str = typer.Option(
        "post", "--option-timing", help="Option pool timing: pre/post"
    ),
    board: str = typer.Option(
        "observer", "--board", "-b", help="Board rights: seat, observer, seat_and_observer, none"
    ),
    pro_rata: bool = typer.Option(True, "--pro-rata/--no-pro-rata", help="Include pro rata rights"),
    tokens: bool = typer.Option(False, "--tokens/--no-tokens", help="Include token rights"),
    token_floor: float = typer.Option(50.0, "--token-floor", help="Token floor percentage"),
    token_side_letter: bool = typer.Option(False, "--token-side-letter", help="Token side letter"),
    token_warrant: bool = typer.Option(False, "--token-warrant", help="Token warrant"),
    token_pro_rata: bool = typer.Option(False, "--token-pro-rata", help="Pro rata on tokens"),
    legal_fee_cap: float = typer.Option(75000.0, "--legal-fee", help="Legal fee cap"),
    exclusivity: int = typer.Option(45, "--exclusivity", help="Exclusivity period in days"),
    custom_terms: Optional[str] = typer.Option(None, "--custom", help="Custom terms"),
    founder: Optional[str] = typer.Option(None, "--founder", help="Founder name"),
    dri: Optional[str] = typer.Option(None, "--dri", help="DRI name"),
    requester_id: str = typer.Option("", "--requester-id", help="Slack user ID of requester"),
    requester_name: str = typer.Option("", "--requester-name", help="Slack username of requester"),
    slack_channel: str = typer.Option("", "--slack-channel", help="Slack channel ID"),
    slack_thread: str = typer.Option("", "--slack-thread", help="Slack thread timestamp"),
    output_format: str = typer.Option(
        "text", "--format", "-f", help="Output format: text, docx, json"
    ),
    output_file: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path"),
    template_file: Optional[str] = typer.Option(
        None,
        "--template",
        "-t",
        help="Path to .docx template file to fill (uses placeholders like {{COMPANY_NAME}})",
    ),
    save_deal: bool = typer.Option(False, "--save", help="Save as a deal for tracking"),
):
    """Create a new term sheet."""
    try:
        instrument_type = InstrumentType(instrument.lower())
    except ValueError:
        console.print(f"[red]Invalid instrument type: {instrument}[/red]")
        console.print("Valid types: safe, priced, convertible_note")
        raise typer.Exit(1)

    try:
        board_rights = BoardRights(board.lower())
    except ValueError:
        console.print(f"[red]Invalid board rights: {board}[/red]")
        console.print("Valid types: seat, observer, none")
        raise typer.Exit(1)

    token_rights = TokenRights(
        enabled=tokens,
        side_letter=token_side_letter,
        warrant=token_warrant,
        pro_rata_on_tokens=token_pro_rata,
        token_floor_percent=token_floor,
    )

    term_sheet = client.create_term_sheet(
        company_name=company,
        investment_amount=amount,
        instrument_type=instrument_type,
        valuation_cap=valuation_cap,
        discount_percent=discount,
        pre_money_valuation=pre_money,
        post_money_valuation=post_money,
        series=series,
        option_pool_percent=option_pool,
        option_pool_timing=option_timing,
        board_rights=board_rights,
        pro_rata_rights=pro_rata,
        token_rights=token_rights,
        legal_fee_cap=legal_fee_cap,
        exclusivity_days=exclusivity,
        custom_terms=custom_terms or "",
        founder_name=founder or "",
        dri_name=dri or "",
    )

    if save_deal and requester_id:
        deal = client.create_deal(
            company_name=company,
            term_sheet=term_sheet,
            requester_user_id=requester_id,
            requester_user_name=requester_name,
            slack_channel=slack_channel,
            slack_thread_ts=slack_thread,
        )
        console.print(f"[green]Created deal: {deal.id}[/green]")

    if output_format == "text":
        text = client.generate_text(term_sheet)
        if output_file:
            Path(output_file).write_text(text)
            console.print(f"[green]Saved to {output_file}[/green]")
        else:
            console.print(text)

    elif output_format == "docx":
        try:
            docx_bytes = client.generate_docx(term_sheet, template_file=template_file)
            if template_file:
                console.print(f"[blue]Using template: {template_file}[/blue]")
        except FileNotFoundError:
            console.print(f"[red]Template file not found: {template_file}[/red]")
            raise typer.Exit(1)
        output_path = output_file or f"{company.replace(' ', '_')}_Term_Sheet.docx"
        Path(output_path).write_bytes(docx_bytes)
        console.print(f"[green]Saved to {output_path}[/green]")

    elif output_format == "json":
        data = term_sheet.to_dict()
        if output_file:
            Path(output_file).write_text(json.dumps(data, indent=2))
            console.print(f"[green]Saved to {output_file}[/green]")
        else:
            console.print(json.dumps(data, indent=2))


@app.command("list")
def list_term_sheets(
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status"),
    output_format: str = typer.Option("table", "--format", "-f", help="Output format: table, json"),
):
    """List all tracked deals."""
    status_filter = None
    if status:
        try:
            status_filter = DealStatus(status.lower())
        except ValueError:
            console.print(f"[red]Invalid status: {status}[/red]")
            console.print("Valid statuses: draft, pending_approval, approved, sent")
            raise typer.Exit(1)

    deals = client.list_deals(status_filter)

    if not deals:
        console.print("[yellow]No deals found[/yellow]")
        return

    if output_format == "json":
        console.print(json.dumps([d.to_dict() for d in deals], indent=2))
        return

    table = Table(title="Term Sheet Deals")
    table.add_column("ID", style="cyan")
    table.add_column("Company", style="bold")
    table.add_column("Amount")
    table.add_column("Instrument")
    table.add_column("Status")
    table.add_column("Requester")
    table.add_column("Updated")

    for deal in deals:
        ts = deal.term_sheet
        table.add_row(
            deal.id,
            deal.company_name,
            _format_money(ts.investment_amount),
            ts.instrument_type.value,
            f"{_status_emoji(deal.status)} {deal.status.value}",
            deal.requester_user_name or deal.requester_user_id,
            deal.updated_at[:10],
        )

    console.print(table)


@app.command("get")
def get_term_sheet(
    identifier: str = typer.Argument(..., help="Deal ID or company name"),
    output_format: str = typer.Option(
        "text", "--format", "-f", help="Output format: text, docx, json, email"
    ),
    output_file: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path"),
):
    """Get a specific deal by ID or company name."""
    deal = client.get_deal(identifier)

    if not deal:
        console.print(f"[red]Deal not found: {identifier}[/red]")
        raise typer.Exit(1)

    if output_format == "json":
        console.print(json.dumps(deal.to_dict(), indent=2))

    elif output_format == "email":
        email = client.generate_email(deal.term_sheet)
        console.print(email)

    elif output_format == "docx":
        docx_bytes = client.generate_docx(deal.term_sheet)
        output_path = output_file or f"{deal.company_name.replace(' ', '_')}_Term_Sheet.docx"
        Path(output_path).write_bytes(docx_bytes)
        console.print(f"[green]Saved to {output_path}[/green]")

    else:
        console.print(f"[bold]Deal: {deal.id}[/bold]")
        console.print(f"Status: {_status_emoji(deal.status)} {deal.status.value}")
        console.print(f"Requester: {deal.requester_user_name} ({deal.requester_user_id})")
        console.print(f"Created: {deal.created_at}")
        console.print(f"Updated: {deal.updated_at}")
        if deal.approved_at:
            console.print(f"Approved: {deal.approved_at} by {deal.approved_by}")
        if deal.sent_at:
            console.print(f"Sent: {deal.sent_at}")
        console.print()
        console.print(client.generate_text(deal.term_sheet))


@app.command("status")
def check_status(
    company: str = typer.Argument(..., help="Company name to check status"),
):
    """Check the status of a deal by company name."""
    deal = client.get_deal(company)

    if not deal:
        console.print(f"[yellow]No deal found for: {company}[/yellow]")
        raise typer.Exit(1)

    status_text = {
        DealStatus.DRAFT: "Draft - being prepared",
        DealStatus.PENDING_APPROVAL: "Pending approval from @ben",
        DealStatus.APPROVED: "Approved - ready to send",
        DealStatus.SENT: "Sent to company",
    }.get(deal.status, deal.status.value)

    console.print(f"[bold]{deal.company_name}[/bold] ({deal.id})")
    console.print(f"Status: {_status_emoji(deal.status)} {status_text}")
    console.print(f"Requester: {deal.requester_user_name}")
    console.print(f"Amount: {_format_money(deal.term_sheet.investment_amount)}")
    console.print(f"Instrument: {deal.term_sheet.instrument_type.value}")

    if deal.revision_history:
        console.print("\n[bold]Revision History:[/bold]")
        for rev in deal.revision_history:
            console.print(f"  • {rev['timestamp'][:10]}: {rev['note']}")


@app.command("update")
def update_term_sheet(
    deal_id: str = typer.Argument(..., help="Deal ID"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="New status"),
    approved_by: Optional[str] = typer.Option(None, "--approved-by", help="Approver username"),
    note: Optional[str] = typer.Option(None, "--note", "-n", help="Revision note"),
    amount: Optional[float] = typer.Option(None, "--amount", "-a", help="Update investment amount"),
    valuation_cap: Optional[float] = typer.Option(None, "--cap", help="Update valuation cap"),
    post_money: Optional[float] = typer.Option(None, "--post-money", help="Update post-money"),
    board: Optional[str] = typer.Option(None, "--board", help="Update board rights"),
    tokens: Optional[bool] = typer.Option(None, "--tokens/--no-tokens", help="Update token rights"),
    token_floor: Optional[float] = typer.Option(None, "--token-floor", help="Update token floor"),
):
    """Update an existing deal."""
    deal = client.get_deal(deal_id)
    if not deal:
        console.print(f"[red]Deal not found: {deal_id}[/red]")
        raise typer.Exit(1)

    new_status = None
    if status:
        try:
            new_status = DealStatus(status.lower())
        except ValueError:
            console.print(f"[red]Invalid status: {status}[/red]")
            raise typer.Exit(1)

    new_term_sheet = None
    ts = deal.term_sheet
    updated = False

    if amount is not None:
        ts.investment_amount = amount
        updated = True
    if valuation_cap is not None:
        ts.valuation_cap = valuation_cap
        updated = True
    if post_money is not None:
        ts.post_money_valuation = post_money
        updated = True
    if board is not None:
        try:
            ts.board_rights = BoardRights(board.lower())
            updated = True
        except ValueError:
            console.print(f"[red]Invalid board rights: {board}[/red]")
            raise typer.Exit(1)
    if tokens is not None:
        ts.token_rights.enabled = tokens
        updated = True
    if token_floor is not None:
        ts.token_rights.token_floor_percent = token_floor
        updated = True

    if updated:
        new_term_sheet = ts

    result = client.update_deal(
        deal_id=deal_id,
        status=new_status,
        term_sheet=new_term_sheet,
        approved_by=approved_by,
        revision_note=note,
    )

    if result:
        console.print(f"[green]Updated deal: {deal_id}[/green]")
        console.print(f"Status: {_status_emoji(result.status)} {result.status.value}")
    else:
        console.print("[red]Failed to update deal[/red]")


@app.command("approve")
def approve_deal_cmd(
    deal_id: str = typer.Argument(..., help="Deal ID to approve"),
    approved_by: str = typer.Option(..., "--by", "-b", help="Approver username"),
):
    """Approve a deal and move to approved status."""
    deal = client.get_deal(deal_id)
    if not deal:
        console.print(f"[red]Deal not found: {deal_id}[/red]")
        raise typer.Exit(1)

    if deal.status == DealStatus.APPROVED:
        console.print("[yellow]Deal already approved[/yellow]")
        return

    result = client.approve_deal(deal_id, approved_by)

    if result:
        console.print(f"[green]✅ Deal approved: {deal_id}[/green]")
        console.print(f"Company: {result.company_name}")
        console.print(f"Amount: {_format_money(result.term_sheet.investment_amount)}")
        console.print(f"Requester: {result.requester_user_name} ({result.requester_user_id})")


@app.command("submit")
def submit_for_approval_cmd(
    deal_id: str = typer.Argument(..., help="Deal ID to submit"),
):
    """Submit a deal for approval (changes status to pending_approval)."""
    deal = client.get_deal(deal_id)
    if not deal:
        console.print(f"[red]Deal not found: {deal_id}[/red]")
        raise typer.Exit(1)

    result = client.submit_for_approval(deal_id)

    if result:
        console.print(f"[green]⏳ Deal submitted for approval: {deal_id}[/green]")


@app.command("sent")
def mark_sent_cmd(
    deal_id: str = typer.Argument(..., help="Deal ID to mark as sent"),
):
    """Mark a deal as sent to the company."""
    deal = client.get_deal(deal_id)
    if not deal:
        console.print(f"[red]Deal not found: {deal_id}[/red]")
        raise typer.Exit(1)

    result = client.mark_sent(deal_id)

    if result:
        console.print(f"[green]📤 Deal marked as sent: {deal_id}[/green]")


@app.command("delete")
def delete_term_sheet(
    deal_id: str = typer.Argument(..., help="Deal ID to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Delete a deal."""
    deal = client.get_deal(deal_id)
    if not deal:
        console.print(f"[red]Deal not found: {deal_id}[/red]")
        raise typer.Exit(1)

    if not force:
        confirm = typer.confirm(f"Delete deal {deal_id} ({deal.company_name})?")
        if not confirm:
            raise typer.Abort()

    if client.delete_deal(deal_id):
        console.print(f"[green]Deleted deal: {deal_id}[/green]")
    else:
        console.print("[red]Failed to delete deal[/red]")


@app.command("email")
def generate_email(
    identifier: str = typer.Argument(..., help="Deal ID or company name"),
    dri: Optional[str] = typer.Option(None, "--dri", help="DRI name override"),
):
    """Generate a draft email for a deal."""
    deal = client.get_deal(identifier)

    if not deal:
        console.print(f"[red]Deal not found: {identifier}[/red]")
        raise typer.Exit(1)

    email = client.generate_email(deal.term_sheet, dri_name=dri)
    console.print(email)


@app.command("thread")
def get_by_thread(
    channel: str = typer.Argument(..., help="Slack channel ID"),
    thread_ts: str = typer.Argument(..., help="Slack thread timestamp"),
):
    """Get deal by Slack thread."""
    deal = client.get_deal_by_thread(channel, thread_ts)

    if not deal:
        console.print("[yellow]No deal found for this thread[/yellow]")
        raise typer.Exit(1)

    console.print(f"[bold]{deal.company_name}[/bold] ({deal.id})")
    console.print(f"Status: {_status_emoji(deal.status)} {deal.status.value}")


if __name__ == "__main__":
    app()
