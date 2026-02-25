#!/usr/bin/env python3
"""Ingest orchestration for parchiver."""

from __future__ import annotations

import json
from pathlib import Path

from .archive import archive_manifest
from ..db import (
    create_archive,
    create_embedding,
    create_file,
    create_parse,
    create_source,
    get_archive_by_key,
    get_embedding_by_content_hash,
    get_embedding_by_fingerprint,
    get_file_by_hash,
    get_parse_by_content_hash,
    get_parse_by_file_id,
    get_source,
    get_db_connection,
    insert_chunks,
    link_file_source,
    update_source_metadata,
)
from .embed import embed_manifest
from .parse import parse_manifest
from ..telemetry import get_logger, log_event, step_timer
from ..utils import normalize_url

logger = get_logger(__name__)


def _ensure_source(cur, source_type: str, source_url: str, metadata: dict) -> dict:
    with step_timer(logger, "ingest.ensure_source", source_type=source_type, source_url=source_url):
        canonical_url = normalize_url(source_url)
        existing = get_source(cur, source_type, canonical_url)
        if existing:
            if metadata:
                update_source_metadata(cur, existing["id"], metadata)
            return existing
        return create_source(cur, source_type, canonical_url, source_url, metadata)


def _persist_files(cur, source: dict, files: list[dict]) -> list[dict]:
    with step_timer(logger, "ingest.persist_files", source_id=source.get("id"), input_files=len(files)) as step:
        saved = []
        for entry in files:
            if entry.get("status") not in ("ok", "partial"):
                saved.append({"status": "skipped", "reason": entry.get("error"), "file": entry})
                continue

            file_hash = entry.get("file_hash")
            file_context = entry.get("context") or {}
            existing = get_file_by_hash(cur, file_hash)
            if existing:
                link_file_source(cur, existing["id"], source["id"], metadata=file_context or None)
                saved.append({"status": "exists", "file": existing})
                continue

            created = create_file(
                cur,
                file_hash=file_hash,
                filename=entry.get("filename"),
                mime_type=entry.get("mime_type"),
                size_bytes=entry.get("size_bytes"),
                local_path=entry.get("file_path"),
            )
            link_file_source(cur, created["id"], source["id"], metadata=file_context or None)
            saved.append({"status": "created", "file": created})

        counts: dict[str, int] = {}
        for item in saved:
            key = item.get("status", "unknown")
            counts[key] = counts.get(key, 0) + 1
        step.set(output_files=len(saved), status_counts=counts)
        return saved


def ingest_manifest(manifest_path: Path, metadata: dict | None = None) -> dict:
    with step_timer(logger, "ingest.manifest", manifest_path=str(manifest_path)) as overall:
        metadata = metadata or {}
        data = json.loads(manifest_path.read_text())
        source_url = data.get("source_url") or data.get("source")
        source_type = data.get("source_type") or "file"
        source_context = data.get("context") or {}
        files = data.get("files", [])
        overall.set(input_files=len(files), source_type=source_type)

        # Merge source context into metadata for source record
        source_metadata = {**metadata, **source_context}

        # --- Scope 1: persist source + files, check skip_parse ---
        with step_timer(logger, "ingest.scope1_persist"):
            conn = get_db_connection()
            cur = conn.cursor()

            source = None
            if source_url:
                source = _ensure_source(cur, source_type, source_url, source_metadata)
            else:
                source = create_source(cur, source_type, None, None, source_metadata)

            persisted = _persist_files(cur, source, files)

            skip_parse = True
            for saved in persisted:
                if saved.get("status") == "created":
                    skip_parse = False
                    break
                if saved.get("status") == "exists":
                    file_info = saved.get("file", {})
                    file_id = file_info.get("id")
                    if file_id and not get_parse_by_file_id(cur, file_id):
                        skip_parse = False
                        break

            conn.commit()
            cur.close()
            conn.close()

        # --- parse (no DB connection held) ---
        if not skip_parse:
            with step_timer(logger, "ingest.parse_stage"):
                parsed = parse_manifest(manifest_path)
        else:
            parsed = {"status": "skipped", "reason": "no_new_files", "files": []}
        parse_manifest_path = Path("/tmp/parchiver_parse.json")
        parse_manifest_path.write_text(json.dumps(parsed))

        # --- Scope 2: check skip_embed ---
        skip_embed = True
        if parsed.get("status") == "ok":
            with step_timer(logger, "ingest.scope2_check_embed_skip"):
                conn = get_db_connection()
                cur = conn.cursor()
                for entry in parsed.get("files", []):
                    content_hash = entry.get("content_hash")
                    if content_hash and not get_embedding_by_content_hash(cur, content_hash):
                        skip_embed = False
                        break
                cur.close()
                conn.close()

        # --- embed (no DB connection held) ---
        if parsed.get("status") == "ok" and skip_embed:
            embedded = {"status": "skipped", "reason": "all_embeddings_exist", "files": []}
        elif parsed.get("status") == "ok":
            with step_timer(logger, "ingest.embed_stage"):
                embedded = embed_manifest(parse_manifest_path)
        else:
            embedded = {"status": "skipped", "reason": "parse_not_run", "files": []}
        embed_manifest_path = Path("/tmp/parchiver_embed.json")
        embed_manifest_path.write_text(json.dumps(embedded))

        # --- archive (no DB connection held) ---
        if embedded.get("status") == "ok":
            with step_timer(logger, "ingest.archive_stage"):
                archived = archive_manifest(embed_manifest_path)
        else:
            archived = {"status": "skipped", "reason": "embed_not_run", "files": []}

        # --- Scope 3: persist parses, embeddings, archives ---
        with step_timer(logger, "ingest.scope3_db_persist"):
            conn = get_db_connection()
            cur = conn.cursor()

            stored_parses = []
            stored_embeddings = []
            stored_archives = []

            for entry in parsed.get("files", []):
                if entry.get("status") != "ok":
                    continue
                file_info = entry.get("file") or {}
                file_hash = file_info.get("file_hash")
                if not file_hash:
                    continue
                db_file = get_file_by_hash(cur, file_hash)
                if not db_file:
                    continue

                content_hash = entry.get("content_hash")
                existing_parse = get_parse_by_content_hash(cur, content_hash) if content_hash else None
                if not existing_parse:
                    existing_parse = get_parse_by_file_id(cur, db_file["id"])
                if not existing_parse:
                    created_parse = create_parse(
                        cur,
                        file_id=db_file["id"],
                        reducto_file_id=entry.get("reducto_file_id"),
                        parse_job_id=entry.get("parse_job_id"),
                        extract_job_id=entry.get("extract_job_id"),
                        parse_json=entry.get("parse_json"),
                        extract_json=entry.get("extract_json"),
                        parsed_text=entry.get("parsed_text"),
                        content_hash=content_hash,
                    )
                    stored_parses.append({"status": "created", "parse": created_parse})
                else:
                    stored_parses.append({"status": "exists", "parse": existing_parse})

                # Persist Reducto metadata into source record
                reducto_meta = entry.get("metadata")
                if reducto_meta and source:
                    update_source_metadata(cur, source["id"], {"reducto": reducto_meta})

            for entry in embedded.get("files", []):
                if entry.get("status") != "ok":
                    continue
                content_hash = entry.get("content_hash")
                fingerprint = entry.get("fingerprint")
                if not fingerprint:
                    continue
                existing_embedding = get_embedding_by_fingerprint(cur, fingerprint)
                if not existing_embedding:
                    created_embedding = create_embedding(
                        cur,
                        content_hash=content_hash,
                        model=entry.get("model"),
                        dims=entry.get("dims"),
                        fingerprint=fingerprint,
                    )
                    insert_chunks(cur, created_embedding["id"], entry.get("chunks", []))
                    stored_embeddings.append({"status": "created", "embedding": created_embedding})
                else:
                    stored_embeddings.append({"status": "exists", "embedding": existing_embedding})

            for entry in archived.get("files", []):
                if entry.get("status") not in ("ok", "skipped"):
                    continue
                file_info = entry.get("file") or {}
                file_hash = file_info.get("file_hash")
                if not file_hash:
                    continue
                db_file = get_file_by_hash(cur, file_hash)
                if not db_file:
                    continue
                existing_archive = get_archive_by_key(cur, entry.get("r2_bucket"), entry.get("r2_key"))
                if not existing_archive:
                    created_archive = create_archive(
                        cur,
                        file_id=db_file["id"],
                        bucket=entry.get("r2_bucket"),
                        key=entry.get("r2_key"),
                        etag=None,
                        size_bytes=file_info.get("size_bytes"),
                        company_slug=entry.get("company_slug"),
                    )
                    stored_archives.append({"status": "created", "archive": created_archive})
                else:
                    stored_archives.append({"status": "exists", "archive": existing_archive})

            conn.commit()
            cur.close()
            conn.close()

        overall.set(
            parse_status=parsed.get("status"),
            embed_status=embedded.get("status"),
            archive_status=archived.get("status"),
            persisted_files=len(persisted),
            stored_parses=len(stored_parses),
            stored_embeddings=len(stored_embeddings),
            stored_archives=len(stored_archives),
        )
        log_event(
            logger,
            "ingest.completed",
            source_url=source_url,
            parse_status=parsed.get("status"),
            embed_status=embedded.get("status"),
            archive_status=archived.get("status"),
        )
        return {
            "status": "ok",
            "source": source,
            "files": persisted,
            "parse": parsed,
            "embed": embedded,
            "archive": archived,
            "db": {
                "parses": stored_parses,
                "embeddings": stored_embeddings,
                "archives": stored_archives,
            },
        }
