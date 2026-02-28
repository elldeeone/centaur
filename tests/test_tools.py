"""Comprehensive tool test harness.

Validates every loaded tool across 4 layers:
  1. Library import & discovery — _client() factory works, tools are found
  2. Registry & CLI smoke — CLI --help succeeds, aliases resolve
  3. REST route registration — every tool has a POST endpoint
  4. Schema validation — describe_tool returns clean types (no <class '…'>)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.tool_manager import ToolManager

# ---------------------------------------------------------------------------
# Shared fixture: discover all tools once per test session
# ---------------------------------------------------------------------------

_APP_ROOT = Path(__file__).resolve().parent.parent
_PLUGINS_DIR = _APP_ROOT / "tools"


@pytest.fixture(scope="session")
def manager() -> ToolManager:
    mgr = ToolManager(_PLUGINS_DIR)
    mgr.discover()
    return mgr


# ---------------------------------------------------------------------------
# Layer 1 — Library import & discovery
# ---------------------------------------------------------------------------


class TestImportAndDiscovery:
    def tests_loaded(self, manager: ToolManager) -> None:
        """At least some tools should load successfully."""
        assert len(manager.tools) > 0, "No tools were loaded"

    def test_every_has_tools(self, manager: ToolManager) -> None:
        """Every loaded tool should have at least one discovered tool."""
        matrix = manager.tool_test_matrix()
        for entry in matrix:
            assert entry["library_import"] is True, (
                f"Tool {entry['tool']} failed library import"
            )
            assert len(entry["discovered_methods"]) > 0, (
                f"Tool {entry['tool']} has no discovered tools"
            )


# ---------------------------------------------------------------------------
# Layer 2 — Registry & CLI smoke
# ---------------------------------------------------------------------------


class TestRegistryAndCLI:
    def test_registry_integrity(self, manager: ToolManager) -> None:
        """Verify registry integrity for all tools."""
        results = manager.smoke_test_registry()
        failures = [r for r in results if r["status"] != "ok"]
        if failures:
            details = json.dumps(failures, indent=2)
            pytest.fail(f"Registry failures:\n{details}")

    def test_cli_smoke(self, manager: ToolManager) -> None:
        """CLI --help should succeed for every tool that has a cli.py."""
        results = manager.smoke_test_clis(["--help"])
        failures = [r for r in results if r["status"] not in {"ok", "missing_cli"}]
        if failures:
            details = json.dumps(failures, indent=2)
            pytest.fail(f"CLI smoke failures:\n{details}")

    def test_alias_smoke(self, manager: ToolManager) -> None:
        """Script alias --help should succeed for every tool with aliases."""
        results = manager.smoke_test_aliases(["--help"])
        failures = [r for r in results if r["status"] not in {"ok", "missing_aliases"}]
        if failures:
            details = json.dumps(failures, indent=2)
            pytest.fail(f"Alias smoke failures:\n{details}")


# ---------------------------------------------------------------------------
# Layer 3 — REST route registration
# ---------------------------------------------------------------------------


class TestRESTRoutes:
    def test_all_tools_have_routes(self, manager: ToolManager) -> None:
        """Every tool function should have a POST /tools/<tool>/<tool> route."""
        results = manager.smoke_test_rest_routes()
        failures = [r for r in results if r["status"] != "ok"]
        if failures:
            details = json.dumps(failures, indent=2)
            pytest.fail(f"REST route registration failures:\n{details}")

    def test_route_count_matches_tools(self, manager: ToolManager) -> None:
        """Total registered routes should match total method count."""
        results = manager.smoke_test_rest_routes()
        for r in results:
            assert r["registered_methods"] == r["total_methods"], (
                f"Tool {r['tool']}: {r['registered_methods']}/{r['total_methods']} "
                f"methods registered"
            )


# ---------------------------------------------------------------------------
# Layer 4 — Schema validation (MCP describe_tool quality)
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_no_raw_class_types(self, manager: ToolManager) -> None:
        """describe_tool should not contain <class '...'> type strings."""
        results = manager.smoke_test_schemas()
        failures = [r for r in results if r["status"] != "ok"]
        if failures:
            details = json.dumps(failures, indent=2)
            pytest.fail(f"Schema validation failures:\n{details}")

    def test_describe_tool_returns_methods(self, manager: ToolManager) -> None:
        """describe_tool should return a non-empty methods list for each tool."""
        for name in manager.tools:
            schema = manager.describe_tool(name)
            assert "error" not in schema, f"describe_tool({name}) returned error: {schema}"
            assert len(schema.get("methods", [])) > 0, (
                f"describe_tool({name}) returned no methods"
            )

    def test_all_tool_params_have_type(self, manager: ToolManager) -> None:
        """Every parameter should have a type field that is a non-empty string."""
        for name in manager.tools:
            schema = manager.describe_tool(name)
            for tool_schema in schema.get("methods", []):
                for pname, pinfo in tool_schema.get("parameters", {}).items():
                    ptype = pinfo.get("type", "")
                    assert ptype and isinstance(ptype, str), (
                        f"{name}.{tool_schema['name']}.{pname}: "
                        f"missing or invalid type: {ptype!r}"
                    )
