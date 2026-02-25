#!/usr/bin/env python3
"""Embedding adapter for parchiver."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from ..telemetry import get_logger, log_event, step_timer


load_dotenv()

OPENROUTER_API_KEY = os.getenv("PARCHIVER_OPENROUTER_API_KEY")
OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"

EMBEDDING_MODEL = "openai/text-embedding-3-large"
EMBEDDING_DIMS = int(os.getenv("PARCHIVER_EMBEDDING_DIMS", "1024"))
EMBEDDING_BATCH_SIZE = 16
EMBEDDING_MAX_CHARS = 8000
FINGERPRINT_FLOATS = 32
logger = get_logger(__name__)


def _check_env() -> None:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")


def _generate_embeddings(texts: list[str], retries: int = 5) -> list[list[float]]:
    with step_timer(
        logger,
        "embed.generate_embeddings",
        batch_size=len(texts),
        retries=retries,
    ) as step:
        for attempt in range(retries):
            response = requests.post(
                OPENROUTER_EMBEDDINGS_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": EMBEDDING_MODEL,
                    "input": texts,
                    "dimensions": EMBEDDING_DIMS,
                },
            )
            step.set(attempt=attempt + 1, http_status=response.status_code)
            if response.status_code == 429 or response.status_code >= 500 or response.status_code == 404:
                wait = 2 ** attempt
                print(f"[EMBED] API error {response.status_code}, retrying in {wait}s...")
                log_event(
                    logger,
                    "embed.retrying",
                    attempt=attempt + 1,
                    wait_s=wait,
                    http_status=response.status_code,
                )
                time.sleep(wait)
                continue
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data")
            if not isinstance(data, list):
                if attempt < retries - 1:
                    print(f"[EMBED] No 'data' in response, retrying... ({payload})")
                    log_event(logger, "embed.retrying_missing_data", attempt=attempt + 1)
                    time.sleep(2 ** attempt)
                    continue
                # OpenRouter sometimes returns a JSON error payload without a "data" field.
                # Surface the payload so callers see the real cause (rate limit, input too long, etc.).
                raise RuntimeError(f"Unexpected embeddings response (missing 'data'): {payload}")
            step.set(returned_embeddings=len(data))
            return [item["embedding"] for item in data]
        response.raise_for_status()
        raise RuntimeError("Embedding API failed after retries")


def _build_preamble_text(
    reducto_metadata: dict | None,
    source_context: dict | None,
    file_context: dict | None,
) -> str | None:
    """Build a short preamble text from available metadata and context.

    Priority: Reducto-extracted fields first, then source/file context fields.
    Returns None if nothing meaningful is available.
    """
    lines: list[str] = []
    reducto_metadata = reducto_metadata or {}
    source_context = source_context or {}
    file_context = file_context or {}

    # Reducto fields
    company = reducto_metadata.get("company") or {}
    company_name = company.get("name") if isinstance(company, dict) else None
    deal = reducto_metadata.get("deal") or {}
    document = reducto_metadata.get("document") or {}
    summary = reducto_metadata.get("summary") or {}

    if company_name:
        lines.append(f"Company: {company_name}")
    if isinstance(deal, dict) and deal.get("round_label"):
        lines.append(f"Round: {deal['round_label']}")
    if isinstance(document, dict) and document.get("doc_type"):
        lines.append(f"Document type: {document['doc_type']}")
    if isinstance(document, dict) and document.get("title"):
        lines.append(f"Title: {document['title']}")
    if isinstance(summary, dict) and summary.get("one_liner"):
        lines.append(f"Summary: {summary['one_liner']}")
    if isinstance(summary, dict) and summary.get("sector_tags"):
        lines.append(f"Sectors: {', '.join(summary['sector_tags'])}")

    # Context fields - merged file-level over source-level
    merged_context = {**source_context, **file_context}

    # company_hint only if Reducto didn't find a company name
    if not company_name and merged_context.get("company_hint"):
        lines.append(f"Company: {merged_context.pop('company_hint')}")
    else:
        merged_context.pop("company_hint", None)

    well_known = ["sender", "subject", "date", "channel", "thread_id"]
    for key in well_known:
        value = merged_context.pop(key, None)
        if value and isinstance(value, str):
            lines.append(f"{key.replace('_', ' ').title()}: {value}")

    tags = merged_context.pop("tags", None)
    if isinstance(tags, list) and tags:
        lines.append(f"Tags: {', '.join(str(t) for t in tags)}")

    notes = merged_context.pop("notes", None)
    if notes and isinstance(notes, str):
        lines.append(f"Notes: {notes}")

    # Remaining string-valued context keys
    for key, value in sorted(merged_context.items()):
        if isinstance(value, str) and value:
            lines.append(f"{key.replace('_', ' ').title()}: {value}")

    return "\n".join(lines) if lines else None


def _fingerprint_embedding(embedding: list[float]) -> str:
    head = embedding[:FINGERPRINT_FLOATS]
    serialized = ",".join(f"{value:.6f}" for value in head)
    digest = hashlib.sha256(
        f"{EMBEDDING_MODEL}:{EMBEDDING_DIMS}:{serialized}".encode("utf-8")
    ).hexdigest()
    return digest


def embed_manifest(manifest_path: Path) -> dict:
    with step_timer(logger, "embed.manifest", manifest_path=str(manifest_path)) as overall:
        _check_env()
        data = json.loads(manifest_path.read_text())
        source_context = data.get("context")
        files = data.get("files", [])
        results = []
        overall.set(input_files=len(files))

        for entry in files:
            entry_file = entry.get("file")
            with step_timer(logger, "embed.file", file=entry_file) as entry_step:
                if entry.get("status") != "ok":
                    results.append({
                        "status": "error",
                        "error": entry.get("error", "Parse failed"),
                        "file": entry_file,
                    })
                    entry_step.set(result_status="error", reason="upstream_status")
                    continue

                chunks = entry.get("chunks", [])
                if not chunks:
                    results.append({
                        "status": "error",
                        "error": "No chunks to embed",
                        "file": entry_file,
                    })
                    entry_step.set(result_status="error", reason="no_chunks")
                    continue

                # Build preamble chunk from metadata + context
                file_context = entry.get("context")
                preamble_text = _build_preamble_text(
                    entry.get("metadata"), source_context, file_context
                )
                has_preamble = False
                if preamble_text:
                    chunks = [{"page": 0, "chunk_index": -1, "text": preamble_text}] + chunks
                    has_preamble = True

                texts = [chunk["text"][:EMBEDDING_MAX_CHARS] for chunk in chunks]
                embeddings: list[list[float]] = []
                try:
                    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
                        batch = texts[i : i + EMBEDDING_BATCH_SIZE]
                        embeddings.extend(_generate_embeddings(batch))
                except Exception as exc:
                    results.append({
                        "status": "error",
                        "error": f"Embedding failed: {exc}",
                        "file": entry_file,
                    })
                    entry_step.set(result_status="error", reason="embedding_failed")
                    continue

                for chunk, embedding in zip(chunks, embeddings):
                    chunk["embedding"] = embedding

                # Fingerprint uses first *content* chunk (skip preamble) to preserve dedup
                fp_index = 1 if has_preamble and len(embeddings) > 1 else 0
                fingerprint = _fingerprint_embedding(embeddings[fp_index]) if embeddings else None
                results.append(
                    {
                        "status": "ok",
                        "file": entry_file,
                        "chunks": chunks,
                        "content_hash": entry.get("content_hash"),
                        "metadata": entry.get("metadata"),
                        "context": file_context,
                        "model": EMBEDDING_MODEL,
                        "dims": len(embeddings[0]) if embeddings else 0,
                        "fingerprint": fingerprint,
                    }
                )
                entry_step.set(
                    result_status="ok",
                    chunk_count=len(chunks),
                    embedding_count=len(embeddings),
                    has_preamble=has_preamble,
                )

        status_counts: dict[str, int] = {}
        for result in results:
            key = result.get("status", "unknown")
            status_counts[key] = status_counts.get(key, 0) + 1
        overall.set(output_files=len(results), status_counts=status_counts)
        return {
            "status": "ok",
            "source": data.get("source"),
            "context": source_context,
            "files": results,
        }
