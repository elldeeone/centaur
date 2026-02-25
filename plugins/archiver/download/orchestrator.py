#!/usr/bin/env python3
"""Download adapters for parchiver."""

from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path
from typing import Any

from .docsend import route_all_docsends
from .google import (
    DownloadResult,
    download_doc,
    download_drive_file,
    download_folder,
    parse_google_url,
)

from ..utils import (
    FileRecord,
    compute_file_hash,
    detect_mime_type,
    file_record_to_dict,
    normalize_url,
)
from ..telemetry import get_logger, log_event, step_timer


logger = get_logger(__name__)


MANUAL_DOWNLOAD_SUGGESTION = (
    "If this source keeps failing, please download the file or ZIP manually and share it with us."
)


def _with_manual_download_suggestion(error: str | None) -> str | None:
    if not error:
        return error
    if MANUAL_DOWNLOAD_SUGGESTION in error:
        return error
    if error.endswith((".", "!", "?")):
        return f"{error} {MANUAL_DOWNLOAD_SUGGESTION}"
    return f"{error}. {MANUAL_DOWNLOAD_SUGGESTION}"


def _build_file_record(
    path: Path,
    source_url: str,
    source_type: str,
    title: str | None = None,
    relative_path: str | None = None,
    status: str = "ok",
    error: str | None = None,
) -> FileRecord:
    file_hash = compute_file_hash(path)
    size_bytes = path.stat().st_size
    mime_type = detect_mime_type(path)
    return FileRecord(
        source_url=source_url,
        source_type=source_type,
        file_path=str(path),
        filename=path.name,
        file_hash=file_hash,
        size_bytes=size_bytes,
        mime_type=mime_type,
        title=title,
        relative_path=relative_path,
        status=status,
        error=error,
    )


async def _download_docsend_async(
    source_url: str,
    output_dir: Path,
    company: str | None,
    password: str | None,
) -> dict:
    with step_timer(logger, "download.docsend", source_url=source_url, output_dir=str(output_dir)) as step:
        if not os.getenv("PARCHIVER_BROWSER_USE_API_KEY"):
            return {
                "status": "error",
                "error": _with_manual_download_suggestion("BROWSER_USE_API_KEY not set"),
                "files": [],
            }

        company_name = company or "unknown"
        results = await route_all_docsends(
            [{"url": source_url, "company": company_name, "password": password}],
            output_dir=output_dir,
        )
        if not results:
            return {
                "status": "error",
                "error": _with_manual_download_suggestion("DocSend router returned no results"),
                "files": [],
            }

        result = results[0]
        status = "ok" if result.status.value in ("success", "partial") else "error"
        files: list[dict] = []
        if result.pdf_path:
            path = Path(result.pdf_path)
            if path.exists():
                # Expand ZIP files into individual parseable files
                if path.suffix.lower() == ".zip":
                    expanded = _expand_zip(path, source_url, "docsend", company_name)
                    if expanded:
                        files.extend(expanded)
                    else:
                        # ZIP had no parseable files, keep the ZIP record
                        record = _build_file_record(
                            path, source_url, "docsend", title=company_name,
                            status="ok" if status == "ok" else "partial",
                            error=result.error,
                        )
                        files.append(file_record_to_dict(record))
                else:
                    record = _build_file_record(
                        path, source_url, "docsend", title=company_name,
                        status="ok" if status == "ok" else "partial",
                        error=result.error,
                    )
                    files.append(file_record_to_dict(record))
        step.set(
            result_status=status,
            result_kind=result.status.value,
            total_pages=result.total_pages,
            downloaded=result.downloaded,
            file_count=len(files),
        )
        return {
            "status": status,
            "error": _with_manual_download_suggestion(result.error) if status != "ok" else result.error,
            "files": files,
            "docsend": {
                "total_pages": result.total_pages,
                "downloaded": result.downloaded,
                "failed_slides": result.failed_slides,
            },
        }


_ZIP_PARSEABLE_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".tiff", ".bmp", ".webp",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
}


def _expand_zip(
    zip_path: Path,
    source_url: str,
    source_type: str,
    title: str | None,
) -> list[dict]:
    """Extract a ZIP and return file records for each supported file inside."""
    with step_timer(logger, "download.expand_zip", zip_path=str(zip_path)) as step:
        extract_dir = zip_path.parent / (zip_path.stem + "_extracted")
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        records: list[dict] = []
        for path in sorted(extract_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in _ZIP_PARSEABLE_EXTENSIONS:
                continue
            if path.name.startswith(".") or "__MACOSX" in str(path):
                continue
            relative = str(path.relative_to(extract_dir))
            record = _build_file_record(
                path,
                source_url,
                source_type,
                title=title,
                relative_path=relative,
            )
            records.append(file_record_to_dict(record))
        step.set(file_count=len(records), extract_dir=str(extract_dir))
        return records


def download_docsend(
    source_url: str,
    output_dir: Path,
    company: str | None,
    password: str | None,
) -> dict:
    return asyncio.run(
        _download_docsend_async(
            source_url=source_url,
            output_dir=output_dir,
            company=company,
            password=password,
        )
    )


def _record_from_download_result(
    result: DownloadResult,
    source_url: str,
    source_type: str,
    base_dir: Path,
) -> FileRecord | None:
    if result.status != "ok" or not result.output_path:
        return FileRecord(
            source_url=source_url,
            source_type=source_type,
            file_path=result.output_path or "",
            filename=Path(result.output_path or "unknown").name,
            file_hash="",
            size_bytes=0,
            mime_type=None,
            title=result.title,
            status=result.status if result.status in ("forbidden", "not_found") else "error",
            error=_with_manual_download_suggestion(result.error),
        )

    path = Path(result.output_path)
    relative_path = str(path.relative_to(base_dir)) if path.exists() else None
    return _build_file_record(
        path,
        source_url,
        source_type,
        title=result.title,
        relative_path=relative_path,
    )


def download_google(
    source_url: str,
    output_dir: Path,
    account: str,
    max_depth: int,
) -> dict:
    with step_timer(
        logger,
        "download.google",
        source_url=source_url,
        account=account,
        max_depth=max_depth,
    ) as step:
        parsed = parse_google_url(source_url)
        if not parsed:
            return {
                "status": "error",
                "error": "Unsupported Google URL",
                "files": [],
            }

        file_id, link_type = parsed
        google_dir = output_dir / "google"
        google_dir.mkdir(parents=True, exist_ok=True)

        files: list[dict] = []
        status = "ok"
        error = None

        if link_type == "folder":
            folder_dir = google_dir / "folders" / file_id
            folder_dir.mkdir(parents=True, exist_ok=True)
            results = download_folder(file_id, folder_dir, account, max_depth=max_depth)
            for result in results:
                record = _record_from_download_result(result, source_url, "google_drive", folder_dir)
                if record:
                    files.append(file_record_to_dict(record))
                if result.status != "ok":
                    status = "partial"
        else:
            file_dir = google_dir / "files" / file_id
            file_dir.mkdir(parents=True, exist_ok=True)
            if link_type in ("document", "presentation", "spreadsheets"):
                result = download_doc(file_id, link_type, file_dir, account)
            else:
                result = download_drive_file(file_id, file_dir, account)
            record = _record_from_download_result(result, source_url, "google_drive", file_dir)
            if record:
                files.append(file_record_to_dict(record))
            if result.status != "ok":
                status = "error"
                error = _with_manual_download_suggestion(result.error)

        step.set(result_status=status, link_type=link_type, file_id=file_id, file_count=len(files))
        return {
            "status": status,
            "error": error,
            "files": files,
            "google": {
                "link_type": link_type,
                "file_id": file_id,
            },
        }


def download_source(
    source_url: str,
    output_dir: Path,
    company: str | None,
    account: str | None,
    password: str | None,
    max_depth: int,
) -> dict:
    with step_timer(logger, "download.source", source_url=source_url, output_dir=str(output_dir)) as step:
        canonical_url = normalize_url(source_url)
        output_dir.mkdir(parents=True, exist_ok=True)
        if "docsend.com" in canonical_url:
            docsend_dir = output_dir / "docsend"
            docsend_dir.mkdir(parents=True, exist_ok=True)
            payload = download_docsend(source_url, docsend_dir, company, password)
            result = {
                "status": payload["status"],
                "error": (
                    _with_manual_download_suggestion(payload.get("error"))
                    if payload["status"] != "ok"
                    else payload.get("error")
                ),
                "source_url": source_url,
                "canonical_url": canonical_url,
                "source_type": "docsend",
                "files": payload["files"],
                "details": payload.get("docsend"),
            }
            step.set(source_type="docsend", result_status=result["status"], file_count=len(result["files"]))
            return result

        if "google.com" in canonical_url:
            if not account:
                result = {
                    "status": "error",
                    "error": "Google download requires --account",
                    "source_url": source_url,
                    "canonical_url": canonical_url,
                    "source_type": "google_drive",
                    "files": [],
                }
                step.set(source_type="google_drive", result_status="error", file_count=0)
                return result
            payload = download_google(source_url, output_dir, account, max_depth)
            result = {
                "status": payload["status"],
                "error": (
                    _with_manual_download_suggestion(payload.get("error"))
                    if payload["status"] != "ok"
                    else payload.get("error")
                ),
                "source_url": source_url,
                "canonical_url": canonical_url,
                "source_type": "google_drive",
                "files": payload["files"],
                "details": payload.get("google"),
            }
            step.set(source_type="google_drive", result_status=result["status"], file_count=len(result["files"]))
            return result

        result = {
            "status": "error",
            "error": "Unsupported source type for parchiver download",
            "source_url": source_url,
            "canonical_url": canonical_url,
            "source_type": "unknown",
            "files": [],
        }
        log_event(logger, "download.unsupported_source", source_url=source_url, canonical_url=canonical_url)
        step.set(source_type="unknown", result_status="error", file_count=0)
        return result
