"""ArchiverClient — plugin-pattern wrapper around archiver modules."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .db import init_db
from .download.orchestrator import download_source
from .fetch import fetch_chunk
from .ingest.archive import archive_manifest
from .ingest.embed import embed_manifest
from .ingest.ingest import ingest_manifest
from .ingest.parse import parse_manifest
from .search import search, search_stats
from .status import status_for_source
from .utils import dump_json


class ArchiverClient:
    """Unified client for the document archiver plugin."""

    def init_db(self) -> None:
        init_db()

    def download(
        self,
        source_url: str,
        output_dir: str | Path,
        company: str | None = None,
        account: str | None = None,
        password: str | None = None,
        max_depth: int = 3,
        skip_if_ingested: bool = False,
    ) -> dict:
        output_dir = Path(output_dir)
        if skip_if_ingested:
            status = status_for_source(source_url)
            if status.get("status") == "ok":
                return {
                    "status": "skipped",
                    "reason": "already_ingested",
                    "status_result": status,
                }

        payload = download_source(
            source_url=source_url,
            output_dir=output_dir,
            company=company,
            account=account,
            password=password,
            max_depth=max_depth,
        )
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(dump_json(payload))
        return payload

    def _manifest_with_context(
        self, manifest_path: Path, context: dict | None
    ) -> Path:
        if not context:
            return manifest_path
        data = json.loads(manifest_path.read_text())
        existing = data.get("context") or {}
        data["context"] = {**existing, **context}
        tmp = Path(tempfile.mktemp(suffix=".ctx.json", dir=manifest_path.parent))
        tmp.write_text(json.dumps(data))
        return tmp

    def parse(self, manifest_path: str | Path, context: dict | None = None) -> dict:
        path = Path(manifest_path)
        path = self._manifest_with_context(path, context)
        return parse_manifest(path)

    def embed(self, manifest_path: str | Path, context: dict | None = None) -> dict:
        path = Path(manifest_path)
        path = self._manifest_with_context(path, context)
        return embed_manifest(path)

    def archive(self, manifest_path: str | Path, context: dict | None = None) -> dict:
        path = Path(manifest_path)
        path = self._manifest_with_context(path, context)
        return archive_manifest(path)

    def ingest(self, manifest_path: str | Path, context: dict | None = None) -> dict:
        path = Path(manifest_path)
        path = self._manifest_with_context(path, context)
        return ingest_manifest(path)

    def search(
        self,
        query: str,
        mode: str = "hybrid",
        limit: int = 10,
        threshold: float = 0.3,
    ) -> dict:
        return search(query=query, mode=mode, limit=limit, threshold=threshold)

    def search_stats(self) -> dict:
        return search_stats()

    def status(self, source: str) -> dict:
        return status_for_source(source)

    def fetch(
        self,
        chunk_id: int,
        include_reducto: bool = False,
        download_to: str | None = None,
        overwrite: bool = False,
    ) -> dict:
        return fetch_chunk(
            chunk_id=chunk_id,
            include_reducto=include_reducto,
            download_to=download_to,
            overwrite=overwrite,
        )


def _client() -> ArchiverClient:
    return ArchiverClient()
