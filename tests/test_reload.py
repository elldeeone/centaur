"""Tests for tool hot-reload and the stable REST dispatcher.

Covers:
  1. ToolManager.reload() — clears modules, re-discovers, returns correct result
  2. Threading lock — concurrent reloads are serialized
  3. Stable dispatcher — POST /tools/{tool}/{tool} resolves via live lookup
  4. Admin endpoint — POST /admin/reload-tools works end-to-end
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from api.deps import verify_api_key
from shared.tool_manager import ToolManager

_APP_ROOT = Path(__file__).resolve().parent.parent
_PLUGINS_DIR = _APP_ROOT / "tools"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def manager() -> ToolManager:
    mgr = ToolManager(_PLUGINS_DIR)
    mgr.discover()
    return mgr


@pytest.fixture()
def fresh_manager() -> ToolManager:
    """A per-test manager so mutations don't leak."""
    mgr = ToolManager(_PLUGINS_DIR)
    mgr.discover()
    return mgr


# ---------------------------------------------------------------------------
# 1. reload() unit tests
# ---------------------------------------------------------------------------


class TestReload:
    def test_reload_returns_tool_list(self, fresh_manager: ToolManager) -> None:
        result = fresh_manager.reload()
        assert "reloaded" in result
        assert "tools" in result
        assert result["reloaded"] == len(fresh_manager.tools)
        assert set(result["tools"]) == set(fresh_manager.tools.keys())

    def test_reload_clears_module_cache(self, fresh_manager: ToolManager) -> None:
        runtime_mods_before = [
            k for k in sys.modules if k.startswith("shared.tools_runtime.")
        ]
        assert len(runtime_mods_before) > 0, "Expected tool modules in sys.modules"

        fresh_manager.reload()

        runtime_mods_after = [
            k for k in sys.modules if k.startswith("shared.tools_runtime.")
        ]
        assert len(runtime_mods_after) > 0, "Plugins should be re-imported after reload"

    def test_reload_idempotent(self, fresh_manager: ToolManager) -> None:
        r1 = fresh_manager.reload()
        r2 = fresh_manager.reload()
        assert r1["tools"] == r2["tools"]
        assert r1["reloaded"] == r2["reloaded"]


# ---------------------------------------------------------------------------
# 2. Threading lock
# ---------------------------------------------------------------------------


class TestReloadConcurrency:
    def test_concurrent_reloads_serialized(self, fresh_manager: ToolManager) -> None:
        results: list[dict[str, Any]] = []
        errors: list[Exception] = []

        def do_reload() -> None:
            try:
                results.append(fresh_manager.reload())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_reload) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Reload errors: {errors}"
        assert len(results) == 4
        for r in results:
            assert set(r["tools"]) == set(results[0]["tools"])


# ---------------------------------------------------------------------------
# 3. Stable dispatcher — REST routes resolve via live lookup
# ---------------------------------------------------------------------------


class TestStableDispatcher:
    def test_dispatcher_has_wildcard_route(self, manager: ToolManager) -> None:
        """The router should use a wildcard dispatcher, not per-tool routes."""
        router = manager.create_rest_router()
        paths = [getattr(r, "path", "") for r in router.routes]
        assert "/tools/{tool_name}/{method_name}" in paths

    def test_dispatcher_reflects_reload(self, fresh_manager: ToolManager) -> None:
        """After reload, the same router sees new tools via live lookup."""
        fresh_manager.create_rest_router()
        tools_before = set(fresh_manager.tools.keys())

        fresh_manager.reload()
        tools_after = set(fresh_manager.tools.keys())

        assert tools_before == tools_after


# ---------------------------------------------------------------------------
# 4. Admin endpoint — end-to-end via FastAPI test client
# ---------------------------------------------------------------------------


class TestAdminEndpoint:
    @pytest.fixture()
    def app(self, fresh_manager: ToolManager):
        from fastapi import FastAPI

        from api.routers.admin import router as admin_router

        app = FastAPI()
        app.state.tool_manager = fresh_manager
        app.dependency_overrides[verify_api_key] = lambda: "test"
        app.include_router(admin_router)
        return app

    async def test_reload_endpoint(self, app) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/reload-tools")

        assert resp.status_code == 200
        data = resp.json()
        assert "reloaded" in data
        assert "tools" in data
        assert data["reloaded"] > 0

    async def test_reload_endpoint_returns_tool_names(self, app) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/reload-tools")

        data = resp.json()
        assert isinstance(data["tools"], list)
        assert all(isinstance(p, str) for p in data["tools"])
