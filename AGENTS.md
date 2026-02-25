# Tempo AI v2

## What This Is

Memory-augmented agent system. Postgres+pgvector data plane, FastAPI+MCP API, 60+ plugins for on-demand tool calls. Designed to be plugged into any Claude/Codex instance as an authenticated MCP server, with Slack as the primary orchestration interface.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  API Service (FastAPI)                                  │
│  ├── /mcp           → MCP tools (Bearer auth)          │
│  ├── /health        → health check                     │
│  ├── /search        → hybrid semantic+keyword search   │
│  ├── /plugins/*     → REST endpoints per plugin tool   │
│  └── /secrets       → secret management                │
├─────────────────────────────────────────────────────────┤
│  Plugin System (60+ plugins in plugins/)                │
│  ├── On-demand tools: archiver, twitter, allium, etc.  │
│  └── Indexed sources: slack, linear, gsuite, github    │
├─────────────────────────────────────────────────────────┤
│  ETL Pipeline (src/ai_v2/pipeline.py + extractors/)    │
│  └── Backfill/frontfill into PG from indexed sources   │
├─────────────────────────────────────────────────────────┤
│  Postgres + pgvector                                    │
│  ├── raw_records (JSONB) — all ingested data            │
│  ├── embeddings — vector search                         │
│  ├── sync_cursors / sync_runs — ETL state               │
│  └── people / entity_mappings — cross-source identity   │
└─────────────────────────────────────────────────────────┘
```

## Structure

- `src/ai_v2/` — Core Python package
  - `app.py` — FastAPI application, MCP mount at `/mcp` with Bearer auth
  - `mcp_server.py` — MCP server (search, sql_query, get_timeline, etc.)
  - `plugin_manager.py` — Plugin discovery, loading, MCP+REST registration
  - `plugin_sdk.py` — SDK for plugin authors (`secret()`, `PluginContext`)
  - `config.py` — All settings via pydantic-settings
  - `db.py` — asyncpg pool, schema bootstrap
  - `cli.py` — Click CLI (`ai-v2 sync`, `ai-v2 serve`, `ai-v2 plugins list`, etc.)
  - `pipeline.py` — ETL pipeline orchestrator
  - `extractors/` — Source extractors (Slack, Linear, GitHub, GCal, Gmail, etc.)
  - `routers/` — FastAPI route handlers (search, query, sync, secrets, health)
- `plugins/` — 60+ self-contained plugins (see Plugin System below)
- `migrations/` — Alembic PG migrations
- `scripts/` — Deployment and migration scripts
- `sandbox/` — Dockerfile + entrypoint for sandbox images

## Commands

```bash
make install                    # Install all deps (uv sync)
make lint                       # Lint (ruff check + format --check)
make fmt                        # Auto-fix lint + format
make test                       # Run tests (pytest)
make migrate                    # Run Postgres migrations (alembic)
make sync                       # Run ETL pipeline
make api                        # Start API server
```

## CLI

```bash
ai-v2 sync [--source slack]     # Run ETL pipeline
ai-v2 serve                    # Start API server (uvicorn)
ai-v2 embed [--source slack]    # Generate/refresh embeddings
ai-v2 search "query"           # Test hybrid search
ai-v2 status                   # Show sync status
ai-v2 continuous               # Run continuous sync loop
ai-v2 plugins list             # List discovered plugins and tools
ai-v2 plugins run <tool> ...   # Run a plugin CLI
ai-v2 plugins test             # Smoke-test all plugins
```

## Plugin System

### Structure

Every plugin lives in `plugins/<name>/` with this layout:

```
plugins/
  my-plugin/
    __init__.py      # Single-line docstring
    pyproject.toml   # [tool.ai-v2-plugin] section
    .env.example     # Document required secrets
    client.py        # API client class + _client() factory
    cli.py           # Typer CLI for standalone use
```

### How Tools Are Discovered

The `PluginManager` scans `plugins/`, reads each `pyproject.toml` for `[tool.ai-v2-plugin] module = "client.py"`, calls the `_client()` factory, and exposes every public method of the returned class as:
- **MCP tool** (via `mcp.tool()`)
- **REST endpoint** (`POST /plugins/{plugin}/{tool}`)
- **CLI command** (`ai-v2 plugins run {plugin} ...`)

### Writing a Plugin

**`client.py`** — must have a class + `_client()` factory:
```python
class MyClient:
    def search(self, query: str, limit: int = 10) -> dict:
        """Search something. Docstring becomes tool description."""
        api_key = os.getenv("MY_API_KEY", "")
        # ... implementation ...
        return {"results": [...]}

def _client() -> MyClient:
    return MyClient()
```

**`pyproject.toml`**:
```toml
[project]
name = "ai-v2-plugin-my-plugin"
description = "What this plugin does"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["httpx>=0.28.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ai-v2-plugin]
module = "client.py"
```

**`cli.py`** — thin typer wrapper over the client:
```python
from dotenv import load_dotenv
load_dotenv()

import typer
from .client import _client

app = typer.Typer(name="my-plugin", help="What this plugin does")
client = _client()

@app.command()
def search(query: str, limit: int = 10):
    """Search something."""
    result = client.search(query, limit)
    print(json.dumps(result, indent=2))
```

### Secrets

Resolution order:
1. Plugin `.env` (`plugins/<name>/.env`) — per-plugin overrides
2. Root `.env` (repo root) — centralized secrets
3. Environment variables — Docker/k8s/sops/1pw

All secrets are loaded by the `PluginManager` and injected into `PluginContext`. Plugin code can use `os.getenv()` or the `secret()` helper from `ai_v2.plugin_sdk`. The `cli.py` files use `load_dotenv()` for standalone execution.

### Conventions

- `client.py`: NO `load_dotenv()`. Secrets via `os.getenv()` or `secret()`.
- `cli.py`: YES `load_dotenv()` at top (standalone support). Use `from ai_v2.cli_tables import Table` for rich tables.
- Methods starting with `_` or lifecycle methods (`close`, `connect`, `shutdown`) are excluded from tool registration.
- Stub clients (for CLI-only tools) are acceptable but should have docstrings explaining planned capabilities.

## Rules

- Python 3.11+, use `uv` for all dependency management — never pip/poetry/pipenv
- `ruff` for linting and formatting (line-length=100)
- All secrets via environment variables, never hardcode credentials
- Use `asyncpg` for Postgres connections, `pgvector` for embeddings
- No staging views or mart views — query `raw_records` JSONB directly via `data->>'field'`
- Alembic for all schema migrations — never modify the DB manually
- All API endpoints require `Authorization: Bearer <key>` auth
- Tests use pytest with pytest-asyncio in `tests/` directory
- Follow conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`
- Only extract company knowledge base sources (Slack, Linear, GitHub, GCal, Gmail, GDrive, Granola, Attio, Pylon, BetterStack) into the ETL pipeline
- External data tools (allium, defillama, twitter, archiver, etc.) are on-demand plugins — not stored in raw_records

## CI

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run mypy src/ai_v2
```

## Running Locally

```bash
# 1. Start Postgres
docker compose up -d postgres

# 2. Run migrations
make migrate

# 3. Start API (loads all plugins, serves MCP at /mcp)
make api
# → http://localhost:8000/mcp (Bearer auth with API_SECRET_KEY)

# 4. Connect from Claude Desktop — add to MCP config:
#    URL: http://localhost:8000/mcp
#    Headers: { "Authorization": "Bearer <API_SECRET_KEY>" }
```
