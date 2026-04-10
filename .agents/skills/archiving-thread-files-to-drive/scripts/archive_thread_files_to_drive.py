#!/usr/bin/env python3
"""Archive every file from a Slack thread into Google Drive."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from urllib import error, request


THREAD_URL_RE = re.compile(r"/archives/(?P<channel>C[0-9A-Z]+)/p(?P<packed_ts>\d{10,})")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-url")
    parser.add_argument("--channel-id")
    parser.add_argument("--thread-ts")
    parser.add_argument("--folder-id")
    parser.add_argument("--parent-folder-id")
    parser.add_argument("--folder-name")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--api-url", default=os.environ.get("CENTAUR_API_URL", "http://api:8000"))
    parser.add_argument("--api-key", default=os.environ.get("CENTAUR_API_KEY"))
    return parser


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _parse_thread_url(thread_url: str) -> tuple[str, str]:
    match = THREAD_URL_RE.search(thread_url)
    if not match:
        _fail(f"Unsupported Slack thread URL: {thread_url}")

    packed_ts = match.group("packed_ts")
    if len(packed_ts) <= 6:
        _fail(f"Could not parse Slack timestamp from URL: {thread_url}")

    return match.group("channel"), f"{packed_ts[:-6]}.{packed_ts[-6:]}"


class ToolClient:
    def __init__(self, api_url: str, api_key: str | None):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    def call(self, tool: str, method: str, payload: dict) -> dict | list | str:
        body = json.dumps(payload).encode()
        req = request.Request(
            f"{self.api_url}/tools/{tool}/{method}",
            data=body,
            headers={
                "Content-Type": "application/json",
                **(
                    {"Authorization": f"Bearer {self.api_key}"}
                    if self.api_key
                    else {}
                ),
            },
            method="POST",
        )
        try:
            with request.urlopen(req) as response:
                data = json.loads(response.read().decode())
        except error.HTTPError as exc:
            detail = exc.read().decode()
            raise RuntimeError(f"{tool}.{method} failed: HTTP {exc.code} {detail}") from exc

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"{tool}.{method} failed: {data['error']}")
        if isinstance(data, dict) and {"tool", "method", "result"}.issubset(data):
            return _parse_toon_result(data["result"])
        return data


def _parse_toon_result(result: str):
    if not isinstance(result, str):
        return result

    stripped = result.strip()
    if not stripped:
        return ""

    if stripped[0] in "[{":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    lines = stripped.splitlines()
    table_match = re.match(r"^\[(?P<count>\d+)]\{(?P<columns>[^}]*)}:$", lines[0])
    if table_match:
        columns = [column.strip() for column in table_match.group("columns").split(",")]
        rows = []
        for line in lines[1:]:
            if not line.startswith("  "):
                continue
            values = next(csv.reader([line[2:]]))
            rows.append({column: value for column, value in zip(columns, values, strict=False)})
        return rows

    object_lines = []
    for line in lines:
        if re.match(r"^[A-Za-z0-9_]+(?:\[\d+])?:", line):
            object_lines.append(line)
        else:
            object_lines = []
            break
    if object_lines:
        parsed: dict[str, object] = {}
        for line in object_lines:
            key_part, value_part = line.split(":", 1)
            value = value_part.strip().strip('"')
            indexed_match = re.match(r"^(?P<key>[A-Za-z0-9_]+)\[(?P<index>\d+)]$", key_part)
            if indexed_match:
                parsed.setdefault(indexed_match.group("key"), [])
                cast_value = parsed[indexed_match.group("key")]
                assert isinstance(cast_value, list)
                cast_value.append(value)
            else:
                parsed[key_part] = value
        return parsed

    return stripped


def _sanitize_filename(name: str) -> str:
    cleaned = name.replace("/", "_").replace("\\", "_").strip()
    return cleaned or "untitled"


def _collect_files(client: ToolClient, channel_id: str, thread_ts: str) -> list[dict]:
    replies = client.call(
        "slack",
        "get_thread_replies",
        {"channel_id": channel_id, "thread_ts": thread_ts, "limit": 200},
    )
    seen_file_ids: set[str] = set()
    files: list[dict] = []

    for message in replies:
        message_ts = message["timestamp"]
        message_files = client.call(
            "slack",
            "get_message_files",
            {"channel_id": channel_id, "message_ts": message_ts},
        )
        for file_info in message_files:
            file_id = file_info.get("id", "")
            if not file_id or file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)
            files.append(
                {
                    **file_info,
                    "message_ts": message_ts,
                    "message_user": message.get("user", ""),
                }
            )

    return files


def _ensure_destination_folder(
    client: ToolClient,
    folder_id: str | None,
    parent_folder_id: str | None,
    folder_name: str | None,
    dry_run: bool,
) -> dict | None:
    if folder_id:
        if dry_run:
            return {"id": folder_id, "name": folder_name or "existing-folder", "web_view_link": ""}
        return client.call("gsuite", "drive_get", {"file_id": folder_id})

    if not parent_folder_id or not folder_name:
        _fail("Provide either --folder-id or both --parent-folder-id and --folder-name")

    if dry_run:
        return {
            "id": None,
            "name": folder_name,
            "web_view_link": "",
            "parent_ids": [parent_folder_id],
        }

    return client.call(
        "gsuite",
        "drive_create_folder",
        {"name": folder_name, "parent_id": parent_folder_id},
    )


def main() -> int:
    args = _build_parser().parse_args()

    channel_id = args.channel_id
    thread_ts = args.thread_ts
    if args.thread_url:
        channel_id, thread_ts = _parse_thread_url(args.thread_url)

    if not channel_id or not thread_ts:
        _fail("Provide --thread-url or both --channel-id and --thread-ts")

    client = ToolClient(api_url=args.api_url, api_key=args.api_key)
    files = _collect_files(client, channel_id, thread_ts)
    manifest: dict = {
        "thread": {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "thread_url": args.thread_url,
        },
        "files_found": len(files),
        "files": [
            {
                "slack_file_id": file_info.get("id", ""),
                "name": file_info.get("name", ""),
                "message_ts": file_info.get("message_ts", ""),
                "message_user": file_info.get("message_user", ""),
                "size": file_info.get("size", 0),
            }
            for file_info in files
        ],
        "dry_run": args.dry_run,
        "destination": None,
        "uploaded": [],
        "errors": [],
    }

    if not files:
        print(json.dumps(manifest, indent=2))
        return 0

    destination = _ensure_destination_folder(
        client,
        folder_id=args.folder_id,
        parent_folder_id=args.parent_folder_id,
        folder_name=args.folder_name,
        dry_run=args.dry_run,
    )
    manifest["destination"] = destination

    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return 0

    temp_root = Path("/tmp/centaur-thread-archive") / channel_id / thread_ts.replace(".", "_")

    for index, file_info in enumerate(files, start=1):
        file_name = _sanitize_filename(file_info.get("name") or file_info.get("title") or file_info["id"])
        local_path = temp_root / f"{index:02d}-{file_info['id']}-{file_name}"
        try:
            client.call(
                "slack",
                "download_file",
                {"url": file_info["url_private"], "output_path": str(local_path)},
            )
            upload_result = client.call(
                "gsuite",
                "drive_upload",
                {
                    "file_path": str(local_path),
                    "name": file_name,
                    "folder_id": destination["id"],
                },
            )
            manifest["uploaded"].append(
                {
                    "slack_file_id": file_info["id"],
                    "name": file_name,
                    "message_ts": file_info["message_ts"],
                    "drive_file_id": upload_result.get("id", ""),
                    "drive_web_view_link": upload_result.get("web_view_link", ""),
                }
            )
        except Exception as exc:  # pragma: no cover - exercised via live tool calls
            manifest["errors"].append(
                {
                    "slack_file_id": file_info.get("id", ""),
                    "name": file_name,
                    "error": str(exc),
                }
            )

    print(json.dumps(manifest, indent=2))
    return 1 if manifest["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
