from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from centaur_sdk import ToolContext, reset_tool_context, set_tool_context


REPO_ROOT = Path(__file__).resolve().parents[3]
HARMONIC_CLIENT_PATH = REPO_ROOT / "tools" / "research" / "harmonic" / "client.py"


def _load_harmonic_module():
    spec = importlib.util.spec_from_file_location("test_harmonic_client_module", HARMONIC_CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_harmonic_client_factory_seeds_and_cleans_tool_context_secret() -> None:
    token = set_tool_context(
        ToolContext(
            name="harmonic",
            secrets={"HARMONIC_API_KEY": "=== Harmonic ===\nreal-harmonic-key\n# copied from 1Password"},
        )
    )
    try:
        module = _load_harmonic_module()
        client = module._client()
    finally:
        reset_tool_context(token)

    assert client._get_api_key() == "real-harmonic-key"
