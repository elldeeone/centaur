#!/usr/bin/env python3
"""R2 archive adapter for parchiver."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import boto3
from dotenv import load_dotenv

from ..telemetry import get_logger, step_timer
from ..utils import slugify


load_dotenv()

R2_BUCKET = os.getenv("PARCHIVER_R2_BUCKET", "paradigm-ai-archive")
R2_PREFIX = os.getenv("PARCHIVER_R2_PREFIX", "prod-v0")
logger = get_logger(__name__)


def _client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("PARCHIVER_R2_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("PARCHIVER_R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("PARCHIVER_R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _r2_key(company_slug: str, archived_date: str, file_hash: str, filename: str) -> str:
    return f"{R2_PREFIX}/{company_slug}/{archived_date}/{file_hash}/{filename}"


def archive_manifest(manifest_path: Path) -> dict:
    with step_timer(logger, "archive.manifest", manifest_path=str(manifest_path), bucket=R2_BUCKET) as overall:
        client = _client()
        data = json.loads(manifest_path.read_text())
        files = data.get("files", [])
        results = []
        archived_date = datetime.utcnow().strftime("%Y-%m-%d")
        overall.set(input_files=len(files))

        for entry in files:
            file_info = entry.get("file") or entry
            file_path = Path(file_info.get("file_path"))
            with step_timer(
                logger,
                "archive.file",
                file_hash=file_info.get("file_hash"),
                filename=file_info.get("filename"),
            ) as file_step:
                if not file_path.exists():
                    results.append({
                        "status": "error",
                        "error": "File not found",
                        "file": file_info,
                    })
                    file_step.set(result_status="error", reason="file_not_found")
                    continue

                # Company name fallback chain:
                # 1. Reducto metadata.company.name
                # 2. File-level context.company_hint
                # 3. Source-level data.context.company_hint
                # 4. "unknown"
                company_name = None
                metadata = entry.get("metadata") if isinstance(entry, dict) else None
                if isinstance(metadata, dict):
                    company = metadata.get("company") or {}
                    if isinstance(company, dict):
                        company_name = company.get("name")

                if not company_name:
                    file_ctx = entry.get("context") if isinstance(entry, dict) else None
                    if isinstance(file_ctx, dict):
                        company_name = file_ctx.get("company_hint")

                if not company_name:
                    source_ctx = data.get("context")
                    if isinstance(source_ctx, dict):
                        company_name = source_ctx.get("company_hint")

                company_slug = slugify(company_name or "unknown")
                r2_key = _r2_key(company_slug, archived_date, file_info.get("file_hash"), file_info.get("filename"))

                try:
                    client.head_object(Bucket=R2_BUCKET, Key=r2_key)
                    results.append({
                        "status": "skipped",
                        "reason": "already exists",
                        "file": file_info,
                        "r2_bucket": R2_BUCKET,
                        "r2_key": r2_key,
                        "company_slug": company_slug,
                    })
                    file_step.set(result_status="skipped", r2_key=r2_key)
                    continue
                except client.exceptions.NoSuchKey:
                    pass
                except Exception:
                    pass

                client.upload_file(str(file_path), R2_BUCKET, r2_key)
                results.append({
                    "status": "ok",
                    "file": file_info,
                    "r2_bucket": R2_BUCKET,
                    "r2_key": r2_key,
                    "company_slug": company_slug,
                })
                file_step.set(result_status="ok", r2_key=r2_key)

        status_counts: dict[str, int] = {}
        for result in results:
            key = result.get("status", "unknown")
            status_counts[key] = status_counts.get(key, 0) + 1
        overall.set(output_files=len(results), status_counts=status_counts)
        return {
            "status": "ok",
            "source": data.get("source"),
            "context": data.get("context"),
            "files": results,
        }
