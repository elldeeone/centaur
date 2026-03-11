from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.db import ensure_sandbox_schema
from api.routers import admin, health, internal
from api.routers import agent as agent_router_mod
from api.warm_pool import start_replenish_loop, stop_replenish_loop
from api.config import settings
from api.db import close_pool, create_pool
from api.logging_config import configure_structlog
from api.tool_manager import ToolManager, load_plugins_config

configure_structlog()

log = structlog.get_logger().bind(service="api")

# ---------------------------------------------------------------------------
# Uvicorn access/error log → JSON stdout (same schema as structlog)
# ---------------------------------------------------------------------------


class _UvicornJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                "level": record.levelname.lower(),
                "service": "api",
                "event": "http_request",
                "msg": record.getMessage(),
            }
        )


for _uvi_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _uvi_logger = logging.getLogger(_uvi_name)
    _uvi_logger.handlers = [logging.StreamHandler(sys.stdout)]
    _uvi_logger.handlers[0].setFormatter(_UvicornJsonFormatter())
    _uvi_logger.propagate = False


def _warm_tool_caches() -> None:
    """Pre-warm slow tool caches in background thread."""
    import threading

    def _warm() -> None:
        try:
            slack_tool = tool_manager.tools.get("slack")
            if not slack_tool or not slack_tool.methods:
                return
            client = slack_tool.methods[0].fn.__self__
            client._get_user_cache()
            client.list_bot_channels()
            log.info("slack_cache_warmed")
        except Exception as e:
            log.warning("slack_cache_warm_failed", error=str(e))

    threading.Thread(target=_warm, daemon=True).start()


async def _watch_tools(pm: ToolManager) -> None:
    """Watch all plugin directories and auto-reload when files change."""
    from starlette.concurrency import run_in_threadpool
    from watchfiles import awatch

    watch_dirs = [d for d in pm.tools_dirs if d.exists()]
    log.info("tool_watcher_started", paths=[str(d) for d in watch_dirs])
    async for changes in awatch(*watch_dirs):
        changed_files = [str(p) for _, p in changes]
        log.info("tool_files_changed", files=changed_files)
        try:
            result = await run_in_threadpool(pm.reload)
            log.info("tools_auto_reloaded", **result)
            await _push_injection_map()
        except Exception as e:
            log.error("tool_auto_reload_failed", error=str(e))


async def _push_injection_map() -> None:
    """Push the tool injection map to the firewall on startup.

    The API depends on the firewall (service_healthy), so the firewall is
    guaranteed to be up.  This eliminates the race condition where the
    firewall polls the API for the map before the API is ready.
    """
    firewall_url = os.environ.get("FIREWALL_HEALTH_URL", "http://firewall:8081")
    injection_map = tool_manager.build_injection_map()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{firewall_url}/injection-map",
                json=injection_map,
                timeout=5,
            )
            resp.raise_for_status()
        log.info(
            "injection_map_pushed",
            hosts=len(injection_map),
            keys=sum(len(v) for v in injection_map.values()),
        )
    except Exception:
        log.warning("injection_map_push_failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.db_pool = await create_pool(settings.database_url)
    await ensure_sandbox_schema(app.state.db_pool)
    _warm_tool_caches()
    await _push_injection_map()
    watcher_task = asyncio.create_task(_watch_tools(tool_manager))
    await start_replenish_loop()
    try:
        yield
    finally:
        await stop_replenish_loop()
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task
        await close_pool(app.state.db_pool)


app = FastAPI(
    title="AI v2 API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(agent_router_mod.router)
app.include_router(admin.router)
app.include_router(internal.router)


# Load tools
# Resolution order: TOOL_DIRS env var (colon-separated) → tools.toml → PLUGINS_DIR fallback
_app_root = Path(__file__).resolve().parent.parent.parent

_tool_dirs_env = os.environ.get("TOOL_DIRS", "")
if _tool_dirs_env:
    _tools_dirs = [Path(d.strip()) for d in _tool_dirs_env.split(":") if d.strip()]
else:
    _plugins_config = _app_root / "tools.toml"
    _plugin_dirs = load_plugins_config(_plugins_config)
    _tools_dirs = (
        _plugin_dirs if _plugin_dirs else [Path(os.environ.get("PLUGINS_DIR", _app_root / "tools"))]
    )

tool_manager = ToolManager(_tools_dirs)
tool_manager.discover()
app.state.tool_manager = tool_manager
app.include_router(tool_manager.create_rest_router())


def get_tool_manager() -> ToolManager:
    return tool_manager
