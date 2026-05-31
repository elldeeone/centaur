"""Parity tests: the local ``centaur-tool`` runner must produce byte-identical
output to the server-side ``ToolManager.call_tool(..., format="toon")`` path.

Both paths share ``centaur_sdk.toolrunner``'s invocation core, so a sample tool
with a fake client returns the same TOON result and the same error envelopes
whether served locally or by the sidecar.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.tool_manager import ToolManager, _parse_secrets  # noqa: E402
from centaur_sdk import toolrunner  # noqa: E402
from centaur_sdk.backends import registry  # noqa: E402
from centaur_sdk.backends.stub import StubBackend  # noqa: E402

FAKE_TOOL_CLIENT = '''
from centaur_sdk import secret


class FakeClient:
    def sync_echo(self, text: str) -> dict:
        return {"mode": "sync", "text": text}

    async def async_echo(self, text: str) -> dict:
        return {"mode": "async", "text": text}

    def listy(self) -> dict:
        return {"rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}], "count": 2}

    def secret_values(self) -> dict:
        return {
            "required": secret("REQ_TOKEN"),
            "optional": secret("OPT_TOKEN", default="missing"),
        }

    def boom(self) -> dict:
        raise RuntimeError("kaboom")


def _client():
    return FakeClient()
'''


def _write_tool(
    tools_dir: Path,
    name: str,
    *,
    secrets: list | None = None,
    optional_secrets: list | None = None,
) -> Path:
    tool_dir = tools_dir / name
    tool_dir.mkdir(parents=True)
    tool_dir.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                f'name = "{name}"',
                'version = "0.1.0"',
                'description = "Fake test tool"',
                "",
                "[tool.centaur]",
                'module = "client.py"',
                'hosts = ["api.example.com"]',
                f"secrets = {secrets or []!r}",
                f"optional_secrets = {optional_secrets or []!r}",
                "",
            ]
        )
    )
    tool_dir.joinpath("client.py").write_text(FAKE_TOOL_CLIENT)
    return tool_dir


@pytest.fixture(autouse=True)
def _stub_backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(registry, "_backend", StubBackend())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args",
    [
        ("sync_echo", {"text": "hello"}),
        ("async_echo", {"text": "world"}),
        ("listy", {}),
        ("secret_values", {}),
        ("sync_echo", {"texts": "typo"}),  # unexpected-arg validation
        ("sync_echo", {}),  # missing-required validation
        ("sync_echo", {"output_path": "/tmp/x"}),  # forbidden-arg validation
        ("nonexistent", {}),  # method-not-found
        ("boom", {}),  # raised exception
    ],
)
async def test_runner_matches_call_tool_toon(
    tmp_path: Path, method: str, args: dict
):
    tools_dir = tmp_path / "tools"
    _write_tool(tools_dir, "alpha", secrets=["REQ_TOKEN"], optional_secrets=["OPT_TOKEN"])

    manager = ToolManager(tools_dir)
    manager.discover()
    server_out = await manager.call_tool("alpha", method, dict(args), format="toon")

    tool_dir = toolrunner.find_tool_dir([tools_dir], "alpha")
    local_out = toolrunner.run_tool(tool_dir, "alpha", method, dict(args), fmt="toon")

    assert local_out == server_out


def test_replace_mode_placeholders_match_resolve_secrets():
    """The runner's secret placeholders must equal ``_resolve_secrets`` for the
    replace-mode HTTP subset the runner is allowed to inject into ToolContext.
    """
    import asyncio

    entries = [
        "RAW_KEY",
        {"name": "HTTP_REPLACE", "type": "http", "match_headers": ["Authorization"]},
        {"name": "HTTP_CUSTOM", "type": "http", "replacer": "PH", "match_headers": ["X-Api-Key"]},
        {
            "name": "HTTP_INJECT",
            "type": "http",
            "mode": "inject",
            "inject_header": "X-Api-Key",
            "hosts": ["api.example.com"],
        },
        {"name": "DB_DSN", "type": "pg_dsn", "secret_ref": "DB_DSN", "database": "x"},
    ]
    parsed = _parse_secrets(entries, default_hosts=("api.example.com",))
    from api.tool_manager import _resolve_secrets

    server_map = asyncio.run(_resolve_secrets(parsed))
    runner_map = toolrunner.replace_mode_http_placeholders({"secrets": entries})
    assert runner_map == server_map
