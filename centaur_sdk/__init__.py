"""Centaur SDK — lightweight toolkit for building Centaur-compatible tools.

Public API:
    secret(key)       — resolve a secret via the pluggable backend
    Table             — Rich table (re-export for CLI tools)
    render_text_table — plain-text table renderer
"""

from __future__ import annotations

from centaur_sdk.tool_sdk import (
    ToolContext,
    current_thread_key,
    get_tool_context,
    reset_tool_context,
    save_attachment,
    save_attachment_from_path,
    secret,
    set_tool_context,
)

try:
    from centaur_sdk.cli_tables import Table, render_text_table
except ModuleNotFoundError as exc:
    if exc.name != "rich":
        raise

    class Table:  # type: ignore[no-redef]
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("rich is required for centaur_sdk.Table")

    def render_text_table(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("rich is required for centaur_sdk.render_text_table")


__all__ = [
    "Table",
    "ToolContext",
    "current_thread_key",
    "get_tool_context",
    "render_text_table",
    "reset_tool_context",
    "save_attachment",
    "save_attachment_from_path",
    "secret",
    "set_tool_context",
]
