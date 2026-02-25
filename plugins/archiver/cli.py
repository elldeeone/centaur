"""Typer CLI for the document archiver plugin."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from .client import _client
from .utils import dump_json

app = typer.Typer(name="archiver", help="Document archiver for investment materials.")


def _read_context(
    context: str | None,
    context_file: str | None,
) -> dict | None:
    if context and context_file:
        print(dump_json({"status": "error", "error": "Cannot specify both --context and --context-file"}))
        raise typer.Exit(1)
    if context:
        return json.loads(context)
    if context_file:
        return json.loads(Path(context_file).read_text())
    return None


@app.command("init-db")
def init_db() -> None:
    """Initialize database schema."""
    client = _client()
    client.init_db()
    print(dump_json({"status": "ok", "action": "init-db"}))


@app.command()
def download(
    source: str = typer.Option(..., help="Source URL (DocSend or Google Drive)"),
    output: str = typer.Option(..., help="Output directory"),
    company: Optional[str] = typer.Option(None, help="Company name for metadata"),
    account: Optional[str] = typer.Option(None, help="Google account email for gog"),
    password: Optional[str] = typer.Option(None, help="DocSend password if required"),
    max_depth: int = typer.Option(3, help="Google folder recursion depth"),
    skip_if_ingested: bool = typer.Option(False, help="Skip download if source already exists in archive"),
) -> None:
    """Download docsend/drive sources."""
    client = _client()
    payload = client.download(
        source_url=source,
        output_dir=output,
        company=company,
        account=account,
        password=password,
        max_depth=max_depth,
        skip_if_ingested=skip_if_ingested,
    )
    print(dump_json(payload))
    if payload.get("status") not in ("ok", "skipped"):
        raise typer.Exit(1)


@app.command()
def parse(
    manifest: str = typer.Option(..., help="Download manifest JSON"),
    context: Optional[str] = typer.Option(None, help="Inline JSON context"),
    context_file: Optional[str] = typer.Option(None, help="Path to JSON file with context"),
) -> None:
    """Parse local files with Reducto."""
    client = _client()
    ctx = _read_context(context, context_file)
    payload = client.parse(manifest, context=ctx)
    print(dump_json(payload))
    if payload.get("status") != "ok":
        raise typer.Exit(1)


@app.command()
def embed(
    manifest: str = typer.Option(..., help="Parse manifest JSON"),
    context: Optional[str] = typer.Option(None, help="Inline JSON context"),
    context_file: Optional[str] = typer.Option(None, help="Path to JSON file with context"),
) -> None:
    """Generate embeddings from parse output."""
    client = _client()
    ctx = _read_context(context, context_file)
    payload = client.embed(manifest, context=ctx)
    print(dump_json(payload))
    if payload.get("status") != "ok":
        raise typer.Exit(1)


@app.command()
def archive(
    manifest: str = typer.Option(..., help="Parse manifest JSON"),
    context: Optional[str] = typer.Option(None, help="Inline JSON context"),
    context_file: Optional[str] = typer.Option(None, help="Path to JSON file with context"),
) -> None:
    """Archive raw files to R2."""
    client = _client()
    ctx = _read_context(context, context_file)
    payload = client.archive(manifest, context=ctx)
    print(dump_json(payload))
    if payload.get("status") != "ok":
        raise typer.Exit(1)


@app.command()
def ingest(
    manifest: str = typer.Option(..., help="Download manifest JSON"),
    context: Optional[str] = typer.Option(None, help="Inline JSON context"),
    context_file: Optional[str] = typer.Option(None, help="Path to JSON file with context"),
) -> None:
    """Run parse/embed/archive for local files."""
    client = _client()
    ctx = _read_context(context, context_file)
    payload = client.ingest(manifest, context=ctx)
    print(dump_json(payload))
    if payload.get("status") != "ok":
        raise typer.Exit(1)


@app.command()
def search(
    query: Optional[str] = typer.Argument(None, help="Search query"),
    mode: str = typer.Option("hybrid", help="Search mode: hybrid, dense, sparse"),
    limit: int = typer.Option(10, "-k", "--limit", help="Max results"),
    threshold: float = typer.Option(0.3, help="Similarity threshold (0-1)"),
    stats: bool = typer.Option(False, help="Show index statistics"),
) -> None:
    """Search indexed documents."""
    client = _client()
    if stats:
        payload = client.search_stats()
    elif query:
        payload = client.search(query=query, mode=mode, limit=limit, threshold=threshold)
    else:
        print(dump_json({"status": "error", "error": "Query or --stats required"}))
        raise typer.Exit(1)
    print(dump_json(payload))


@app.command()
def status(
    source: str = typer.Option(..., help="Source URL or file hash"),
) -> None:
    """Check existing archive status."""
    client = _client()
    payload = client.status(source)
    print(dump_json(payload))
    if payload.get("status") != "ok":
        raise typer.Exit(1)


@app.command()
def fetch(
    chunk_id: int = typer.Option(..., help="Chunk ID from search results"),
    reducto: bool = typer.Option(False, help="Include full Reducto parse/extract payload"),
    download: Optional[str] = typer.Option(None, help="Download original file to this path or directory"),
    overwrite: bool = typer.Option(False, help="Overwrite destination when downloading"),
) -> None:
    """Fetch full Reducto output or download original file for a chunk result."""
    client = _client()
    payload = client.fetch(
        chunk_id=chunk_id,
        include_reducto=reducto,
        download_to=download,
        overwrite=overwrite,
    )
    print(dump_json(payload))
    if payload.get("status") != "ok":
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
