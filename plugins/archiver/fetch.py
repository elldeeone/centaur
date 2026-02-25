#!/usr/bin/env python3
"""Chunk-oriented retrieval helpers for parchiver."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import boto3

from .db import get_chunk_artifacts, get_db_connection
from .telemetry import get_logger, step_timer


logger = get_logger(__name__)


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("PARCHIVER_R2_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("PARCHIVER_R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("PARCHIVER_R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _resolve_download_path(output_path: Path, filename: str) -> Path:
    if output_path.exists() and output_path.is_dir():
        return output_path / filename

    if not output_path.exists() and output_path.suffix == "":
        output_path.mkdir(parents=True, exist_ok=True)
        return output_path / filename

    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def fetch_chunk(
    chunk_id: int,
    include_reducto: bool = False,
    download_to: str | None = None,
    overwrite: bool = False,
) -> dict:
    with step_timer(
        logger,
        "fetch.chunk",
        chunk_id=chunk_id,
        include_reducto=include_reducto,
        download_requested=bool(download_to),
    ):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            record = get_chunk_artifacts(cur, chunk_id)
        finally:
            cur.close()
            conn.close()

        if not record:
            return {"status": "not_found", "chunk_id": chunk_id}

        file_id = record.get("file_id")
        local_path = record.get("local_path")
        local_exists = bool(local_path and Path(local_path).exists())
        can_download_r2 = bool(record.get("r2_bucket") and record.get("r2_key"))
        has_reducto = bool(record.get("parse_json") or record.get("extract_json") or record.get("parsed_text"))

        payload: dict = {
            "status": "ok",
            "chunk": {
                "chunk_id": record.get("chunk_id"),
                "embedding_id": record.get("embedding_id"),
                "page": record.get("chunk_page"),
                "chunk_index": record.get("chunk_index"),
                "text": record.get("chunk_text"),
                "content_hash": record.get("embedding_content_hash"),
            },
            "file": None,
            "archive": None,
            "capabilities": {
                "has_reducto_output": has_reducto,
                "can_download_local": local_exists,
                "can_download_r2": can_download_r2,
            },
        }

        if file_id:
            payload["file"] = {
                "file_id": file_id,
                "file_hash": record.get("file_hash"),
                "filename": record.get("filename"),
                "mime_type": record.get("mime_type"),
                "size_bytes": record.get("size_bytes"),
                "local_path": local_path,
            }

        if record.get("archive_id"):
            payload["archive"] = {
                "archive_id": record.get("archive_id"),
                "r2_bucket": record.get("r2_bucket"),
                "r2_key": record.get("r2_key"),
                "archived_at": record.get("archived_at"),
            }

        if include_reducto:
            payload["reducto"] = {
                "parse_id": record.get("parse_id"),
                "reducto_file_id": record.get("reducto_file_id"),
                "parse_job_id": record.get("parse_job_id"),
                "extract_job_id": record.get("extract_job_id"),
                "parse_created_at": record.get("parse_created_at"),
                "parse_json": record.get("parse_json"),
                "extract_json": record.get("extract_json"),
                "parsed_text": record.get("parsed_text"),
            }

        if download_to:
            if not file_id:
                return {
                    "status": "error",
                    "error": "No file record found for chunk",
                    "chunk_id": chunk_id,
                    "result": payload,
                }

            target = _resolve_download_path(Path(download_to), record.get("filename") or f"chunk-{chunk_id}.bin")
            if target.exists() and not overwrite:
                return {
                    "status": "error",
                    "error": f"Destination exists: {target}",
                    "hint": "Re-run with --overwrite to replace",
                    "chunk_id": chunk_id,
                    "result": payload,
                }

            if local_exists:
                source_path = Path(local_path)
                if not (target.exists() and source_path.resolve() == target.resolve()):
                    shutil.copy2(source_path, target)
                payload["download"] = {
                    "status": "ok",
                    "source": "local",
                    "path": str(target),
                }
                return payload

            if can_download_r2:
                try:
                    client = _r2_client()
                    client.download_file(record["r2_bucket"], record["r2_key"], str(target))
                except Exception as exc:  # pragma: no cover - network/credentials dependent
                    return {
                        "status": "error",
                        "error": f"R2 download failed: {exc}",
                        "chunk_id": chunk_id,
                        "result": payload,
                    }
                payload["download"] = {
                    "status": "ok",
                    "source": "r2",
                    "path": str(target),
                    "r2_bucket": record.get("r2_bucket"),
                    "r2_key": record.get("r2_key"),
                }
                return payload

            return {
                "status": "error",
                "error": "No local file path or R2 archive available for this chunk",
                "chunk_id": chunk_id,
                "result": payload,
            }

        return payload
