from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("sourcer.py")
SPEC = importlib.util.spec_from_file_location("sourcer_script", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
sourcer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sourcer)


def _write_candidates(tmp_path: Path) -> Path:
    payload = {
        "candidates": [
            {
                "name": "Alex Example",
                "title": "Engineering Manager",
                "company": "Signal Corp",
                "linkedin": "https://www.linkedin.com/in/alex-example",
                "location": "Los Angeles, CA",
                "notes": "Strong technical manager with defense background.",
                "scores": {
                    "title_correspondence": 5,
                    "educational_foundation": 4,
                    "professional_trajectory": 4,
                    "talent_density": 5,
                    "timing_window": 3,
                },
            }
        ]
    }
    input_path = tmp_path / "candidates.json"
    input_path.write_text(json.dumps(payload))
    return input_path


def _write_result(payload: dict) -> dict:
    updated_rows = len(payload["rows"]) + 1
    updated_cells = updated_rows * len(payload["headers"])
    return {
        "updated_rows": updated_rows,
        "updated_cells": updated_cells,
        "row_count": len(payload["rows"]),
        "header_count": len(payload["headers"]),
    }


def test_coerce_spreadsheet_reference_accepts_id_and_url():
    assert sourcer._coerce_spreadsheet_reference("sheet-123") == "sheet-123"
    assert (
        sourcer._coerce_spreadsheet_reference(
            "https://docs.google.com/spreadsheets/d/sheet-456/edit#gid=0"
        )
        == "sheet-456"
    )


def test_publish_appends_refined_tab_with_change_log(tmp_path, monkeypatch, capsys):
    input_path = _write_candidates(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    class FakeToolClient:
        def __init__(self, api_url: str, api_key: str | None):
            assert api_url == "http://api:8000"
            assert api_key is None

        def call(self, tool: str, method: str, payload: dict):
            calls.append((tool, method, payload))
            if method == "sheets_create":
                raise AssertionError("refine flow should not create a new spreadsheet")
            if method == "sheets_write_table":
                return _write_result(payload)
            return {"ok": True}

    monkeypatch.setattr(sourcer, "ToolClient", FakeToolClient)

    args = argparse.Namespace(
        input=str(input_path),
        title="Platform Engineer Sourcer Shortlist",
        share_with=None,
        spreadsheet_id="https://docs.google.com/spreadsheets/d/sheet-123/edit#gid=0",
        tab_name="Refined - LA Denver",
        change_log_entry=[
            "Narrowed the company set to defense-adjacent engineering orgs.",
            "Dropped product-heavy candidates and prioritized hands-on leaders.",
        ],
        top_n=None,
        dry_run=False,
        api_url="http://api:8000",
        api_key=None,
    )

    assert sourcer._command_publish(args) == 0

    assert [method for _, method, _ in calls] == [
        "sheets_add_tab",
        "sheets_write_table",
        "sheets_write_table",
    ]
    assert calls[0][2] == {"spreadsheet_id": "sheet-123", "title": "Refined - LA Denver"}
    assert calls[1][2]["headers"] == sourcer.CHANGE_LOG_HEADERS
    assert calls[1][2]["start_cell"] == "A1"
    assert calls[2][2]["headers"] == sourcer.HEADERS
    assert calls[2][2]["start_cell"] == "A5"

    printed = json.loads(capsys.readouterr().out)
    assert printed["spreadsheet_id"] == "sheet-123"
    assert printed["created_new_sheet"] is False
    assert printed["tab_name"] == "Refined - LA Denver"
    assert printed["change_log"] == args.change_log_entry
    assert printed["table_start_cell"] == "A5"
    assert printed["existing_tab_reused"] is False
    assert printed["change_log_write"]["updated_rows"] == 3
    assert printed["candidate_table_write"]["updated_rows"] == 2


def test_publish_requires_share_recipient_for_new_sheet(tmp_path):
    input_path = _write_candidates(tmp_path)
    args = argparse.Namespace(
        input=str(input_path),
        title="Platform Engineer Sourcer Shortlist",
        share_with=None,
        spreadsheet_id=None,
        tab_name=None,
        change_log_entry=[],
        top_n=None,
        dry_run=True,
        api_url="http://api:8000",
        api_key=None,
    )

    with pytest.raises(SystemExit):
        sourcer._command_publish(args)


def test_publish_requires_change_log_for_existing_sheet(tmp_path):
    input_path = _write_candidates(tmp_path)
    args = argparse.Namespace(
        input=str(input_path),
        title="Platform Engineer Sourcer Shortlist",
        share_with=None,
        spreadsheet_id="sheet-123",
        tab_name="Refined Replay",
        change_log_entry=[],
        top_n=None,
        dry_run=True,
        api_url="http://api:8000",
        api_key=None,
    )

    with pytest.raises(SystemExit):
        sourcer._command_publish(args)


def test_publish_replays_duplicate_existing_tab(tmp_path, monkeypatch, capsys):
    input_path = _write_candidates(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    class FakeToolClient:
        def __init__(self, api_url: str, api_key: str | None):
            pass

        def call(self, tool: str, method: str, payload: dict):
            calls.append((tool, method, payload))
            if method == "sheets_add_tab":
                raise RuntimeError(
                    "gsuite.sheets_add_tab failed: A sheet with the name "
                    "'Refined Replay' already exists."
                )
            if method == "sheets_write_table":
                return _write_result(payload)
            raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(sourcer, "ToolClient", FakeToolClient)

    args = argparse.Namespace(
        input=str(input_path),
        title="Platform Engineer Sourcer Shortlist",
        share_with=None,
        spreadsheet_id="sheet-123",
        tab_name="Refined Replay",
        change_log_entry=["Replay of the same refined slate after a failed publish."],
        top_n=None,
        dry_run=False,
        api_url="http://api:8000",
        api_key=None,
    )

    assert sourcer._command_publish(args) == 0

    assert [method for _, method, _ in calls] == [
        "sheets_add_tab",
        "sheets_write_table",
        "sheets_write_table",
    ]
    printed = json.loads(capsys.readouterr().out)
    assert printed["existing_tab_reused"] is True
    assert printed["spreadsheet_id"] == "sheet-123"
    assert printed["change_log_write"]["updated_rows"] == 2
    assert printed["candidate_table_write"]["updated_rows"] == 2


def test_publish_checks_change_log_write_counts(tmp_path, monkeypatch):
    input_path = _write_candidates(tmp_path)

    class FakeToolClient:
        def __init__(self, api_url: str, api_key: str | None):
            pass

        def call(self, tool: str, method: str, payload: dict):
            if method == "sheets_write_table" and payload["headers"] == sourcer.CHANGE_LOG_HEADERS:
                return {"updated_rows": 1, "updated_cells": 2}
            if method == "sheets_write_table":
                return _write_result(payload)
            return {"ok": True}

    monkeypatch.setattr(sourcer, "ToolClient", FakeToolClient)

    args = argparse.Namespace(
        input=str(input_path),
        title="Platform Engineer Sourcer Shortlist",
        share_with=None,
        spreadsheet_id="sheet-123",
        tab_name="Refined Counts",
        change_log_entry=["One refinement note."],
        top_n=None,
        dry_run=False,
        api_url="http://api:8000",
        api_key=None,
    )

    with pytest.raises(SystemExit):
        sourcer._command_publish(args)


def test_publish_checks_candidate_table_write_counts(tmp_path, monkeypatch):
    input_path = _write_candidates(tmp_path)

    class FakeToolClient:
        def __init__(self, api_url: str, api_key: str | None):
            pass

        def call(self, tool: str, method: str, payload: dict):
            if method != "sheets_write_table":
                return {"ok": True}
            if payload["headers"] == sourcer.HEADERS:
                return {"updated_rows": 1, "updated_cells": len(sourcer.HEADERS)}
            return _write_result(payload)

    monkeypatch.setattr(sourcer, "ToolClient", FakeToolClient)

    args = argparse.Namespace(
        input=str(input_path),
        title="Platform Engineer Sourcer Shortlist",
        share_with=None,
        spreadsheet_id="sheet-123",
        tab_name="Refined Counts",
        change_log_entry=["One refinement note."],
        top_n=None,
        dry_run=False,
        api_url="http://api:8000",
        api_key=None,
    )

    with pytest.raises(SystemExit):
        sourcer._command_publish(args)
