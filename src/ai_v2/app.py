from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import settings
from .db import close_pool, create_pool
from .mcp_server import mcp, set_plugin_manager, set_pool
from .plugin_manager import PluginManager
from .routers import health, query, search, secrets, sync

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("connecting to database", url=settings.database_url.split("@")[-1])
    pool = await create_pool(settings.database_url)
    app.state.pool = pool
    set_pool(pool)
    log.info("database pool created")
    async with mcp.session_manager.run():
        log.info("mcp session manager started")
        yield
    await close_pool(pool)
    log.info("database pool closed")


app = FastAPI(
    title="AI v2 API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(search.router)
app.include_router(query.router)
app.include_router(sync.router)
app.include_router(secrets.router)

# Load plugins before creating MCP starlette app
_app_root = Path(__file__).resolve().parent.parent.parent
_plugins_dir = Path(os.environ.get("PLUGINS_DIR", _app_root / "plugins"))

plugin_manager = PluginManager(_plugins_dir)
plugin_manager.discover()
set_plugin_manager(plugin_manager)
plugin_manager.register_mcp_tools(mcp)
app.include_router(plugin_manager.create_rest_router())

_mcp_starlette = mcp.streamable_http_app()


class _MCPAuthMiddleware:
    """ASGI middleware that validates Bearer token before forwarding to MCP."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope, receive)
            token: str | None = None
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:]

            if not settings.api_secret_key or token != settings.api_secret_key:
                resp = JSONResponse({"detail": "Invalid or missing Bearer token"}, status_code=401)
                await resp(scope, receive, send)
                return

        await _mcp_starlette(scope, receive, send)


app.mount("/mcp", app=_MCPAuthMiddleware())
