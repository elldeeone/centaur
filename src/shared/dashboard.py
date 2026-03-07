"""Helpers for emitting dashboard specs in TOON format."""

import json
from typing import Any

from toon_format import encode as toon_encode

_SCALAR_TYPES = (str, int, float, bool)

_DATA_KEY = "data"
_LIST_JOIN_KEYS = frozenset({"yKeys"})


def _flatten_for_tabular(data: list[dict]) -> list[dict]:
    """Flatten nested dicts in arrays so TOON can use tabular encoding."""
    if not data or not all(isinstance(item, dict) for item in data):
        return data
    keys = set(data[0].keys())
    if not all(set(d.keys()) == keys for d in data):
        return data
    has_nested = any(
        isinstance(v, (dict, list)) for item in data for v in item.values()
    )
    if not has_nested:
        return data
    flat = []
    for item in data:
        row = {}
        for k, v in item.items():
            if isinstance(v, (dict, list)):
                row[k] = json.dumps(v, separators=(",", ":"), default=str)
            else:
                row[k] = v
        flat.append(row)
    return flat


def _encode_data(data: list[dict]) -> str:
    """Encode a list of dicts as TOON, falling back to JSON."""
    try:
        toon = toon_encode(_flatten_for_tabular(data))
        compact_json = json.dumps(data, separators=(",", ":"), default=str)
        return toon if len(toon) <= len(compact_json) else compact_json
    except Exception:
        return json.dumps(data, default=str)


def _format_scalar(value: Any) -> str:
    """Format a scalar value for a TOON key-value line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_component(component: dict) -> str:
    """Render a single component dict into TOON key-value lines."""
    lines: list[str] = []
    for key, value in component.items():
        if key == _DATA_KEY:
            lines.append(f"{key}:")
            lines.append(_encode_data(value))
        elif key in _LIST_JOIN_KEYS and isinstance(value, list):
            lines.append(f"{key}: {','.join(str(v) for v in value)}")
        elif isinstance(value, _SCALAR_TYPES):
            lines.append(f"{key}: {_format_scalar(value)}")
    return "\n".join(lines)


def emit_dashboard(
    title: str,
    components: list[dict],
    layout: str = "single",
) -> str:
    """Build a ```dashboard block from structured component data.

    Args:
        title: Dashboard title
        components: List of component dicts, each with 'type' and type-specific fields
        layout: Layout mode — 'single', 'grid-2', or 'grid-3'

    Returns:
        Complete ```dashboard fenced block as a string
    """
    sections: list[str] = [f"title: {title}\nlayout: {layout}"]
    for component in components:
        sections.append(_render_component(component))
    body = "\n---\n".join(sections)
    return f"```dashboard\n{body}\n```"
