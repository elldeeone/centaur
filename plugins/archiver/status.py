#!/usr/bin/env python3
"""Status lookups for parchiver."""

from __future__ import annotations

from typing import Optional

from .db import (
    find_source_by_url,
    find_file_by_source,
    get_archive_by_key,
    get_db_connection,
    get_file_by_hash,
    get_parse_by_file_id,
)
from .telemetry import get_logger, step_timer


logger = get_logger(__name__)


def status_for_source(source: str) -> dict:
    with step_timer(logger, "status.lookup", source=source):
        try:
            conn = get_db_connection()
            cur = conn.cursor()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

        try:
            file_info = get_file_by_hash(cur, source)
            if file_info:
                parse = get_parse_by_file_id(cur, file_info["id"])
                return {
                    "status": "ok",
                    "file": file_info,
                    "parse": parse,
                }

            source_row = find_source_by_url(cur, source)
            if not source_row:
                return {"status": "not_found"}

            files = find_file_by_source(cur, source_row["id"])
            parsed = []
            for file_row in files:
                parsed.append({
                    "file": file_row,
                    "parse": get_parse_by_file_id(cur, file_row["id"]),
                })

            return {
                "status": "ok",
                "source": source_row,
                "files": parsed,
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        finally:
            conn.close()
