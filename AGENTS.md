# Tempo AI v2

## What This Is

Memory-augmented agent system. Postgres+pgvector data plane, FastAPI+MCP API, 60+ plugins for on-demand tool calls. Designed to be plugged into any Claude/Codex instance as an authenticated MCP server, with Slack as the primary orchestration interface.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Chat SDK (apps/slackbot)                                    │
│  └── Slack adapter → spawn/execute via REST → post result    │
├──────────────────────────────────────────────────────────────┤
│  API Service (FastAPI)                                       │
│  ├── /mcp           → MCP tools (Bearer auth, Docker bypass) │
│  ├── /health        → health check                           │
│  ├── /search        → hybrid semantic+keyword search         │
│  ├── /plugins/*     → REST endpoints per plugin tool         │
│  └── /secrets       → secret management                      │
├──────────────────────────────────────────────────────────────┤
│  Plugin System (60+ plugins in plugins/)                     │
│  ├── On-demand tools: archiver, twitter, allium, etc.        │
│  ├── Indexed sources: slack, linear, gsuite, github          │
│  └── Agent plugin: Docker sandbox lifecycle (see below)      │
├──────────────────────────────────────────────────────────────┤
│  ETL Pipeline (src/etl/pipeline.py + extractors/)            │
│  └── Backfill/frontfill into PG from indexed sources         │
├──────────────────────────────────────────────────────────────┤
│  Postgres + pgvector                                         │
│  ├── raw_records (JSONB) — all ingested data                 │
│  ├── embeddings — vector search                              │
│  ├── sync_cursors / sync_runs — ETL state                    │
│  └── people / entity_mappings — cross-source identity        │
└──────────────────────────────────────────────────────────────┘
```

### Agent Plugin (Cloud Agent)

The agent plugin (`plugins/agent/`) is the cloud coding agent. The Chat SDK
(or any REST caller) triggers it via the API, and it manages Docker containers
running harness CLIs (amp, claude-code, codex).

```
Chat SDK                  API                     Agent Plugin              Docker
  │                        │                          │                       │
  ├─ POST /plugins/agent/spawn ──────────────────────►│                       │
  │                        │                          ├─ docker run ─────────►│
  │                        │                          │   (sibling container) │
  │◄─ { session_id, status } ◄────────────────────────┤                       │
  │                        │                          │                       │
  ├─ POST /plugins/agent/execute ────────────────────►│                       │
  │                        │                          ├─ docker exec ────────►│
  │                        │                          │   amp -x "message"    │
  │                        │                          │◄── result ────────────┤
  │◄─ { result } ◄───────────────────────────────────┤                       │
  │                        │                          │                       │
  │  ┌─────────────────────┼──────────────────────────┼───────────────────────┤
  │  │ Inside the container, the harness (amp/claude) │                       │
  │  │ has MCP configured → calls back to the API:    │                       │
  │  │   Container ──── MCP /mcp/ ───► API            │                       │
  │  │   (search, sql_query, call_plugin, etc.)       │                       │
  │  └─────────────────────┼──────────────────────────┼───────────────────────┘
```

Key details:
- **1 thread = 1 container**: Each Slack thread (or session key) gets its own Docker container
- **Sibling containers**: API uses the mounted Docker socket (`/var/run/docker.sock`) — no docker-in-docker
- **MCP access**: The entrypoint injects MCP configs (amp, claude-code, codex) using `AI_V2_API_KEY` from env
- **Auth bypass**: Requests from Docker bridge networks (`172.17-22.*`) skip Bearer auth on `/mcp`
- **Secrets forwarding**: API forwards `AMP_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN`, `API_SECRET_KEY` into containers
- **Harnesses**: `amp`, `claude-code`, `codex` — each with JSON streaming output parsed by `_extract_result()`

### Agent plugin files

```
plugins/agent/
  __init__.py        # Plugin docstring
  pyproject.toml     # Plugin config (depends on docker>=7.0.0)
  .env.example       # Required secrets
  client.py          # AgentClient: spawn, execute, status, stop, interrupt
  cli.py             # Typer CLI for standalone use
  Dockerfile         # Sandbox image (Ubuntu 24.04 + uv + gh + node + rust + amp)
  entrypoint.sh      # Injects MCP configs from env, optional repo sync
```

## Structure

- `src/api/` — FastAPI + MCP API service
  - `app.py` — FastAPI application, MCP mount at `/mcp` with Bearer auth (Docker bridge bypass)
  - `mcp_server.py` — MCP server (search, sql_query, get_timeline, etc.)
  - `deps.py` — FastAPI dependencies (auth, pool, embedding service)
  - `routers/` — FastAPI route handlers (search, query, sync, secrets, health)
- `src/etl/` — ETL pipeline service
  - `config.py` — ETL-specific settings (extractor credentials)
  - `pipeline.py` — ETL pipeline orchestrator
  - `embeddings.py` — Embedding generation and hybrid search
  - `extractors/` — Source extractors (Slack, Linear, GitHub, GCal, Gmail, etc.)
- `src/shared/` — Shared code used by both api and etl
  - `config.py` — Base settings via pydantic-settings
  - `db.py` — asyncpg pool, schema bootstrap
  - `cli.py` — Click CLI (`ai-v2 sync`, `ai-v2 serve`, `ai-v2 plugins list`, etc.)
  - `plugin_manager.py` — Plugin discovery, loading, MCP+REST registration
  - `plugin_sdk.py` — SDK for plugin authors (`secret()`, `PluginContext`)
  - `models.py` — Shared data models
  - `cursors.py` — Sync cursor management
- `apps/slackbot/` — Chat SDK bot (Next.js, Slack adapter, Redis state)
  - `src/lib/bot.ts` — Chat SDK wiring: onMention → spawn → execute → post
  - `src/lib/harness.ts` — REST client for the agent plugin API
- `plugins/` — 60+ self-contained plugins (see Plugin System below)
- `plugins/agent/` — Cloud agent sandbox (Dockerfile, entrypoint, container lifecycle)
- `migrations/` — Alembic PG migrations
- `scripts/` — Deployment and migration scripts

## Commands

```bash
make install                    # Install all deps (uv sync)
make lint                       # Lint (ruff check + format --check)
make fmt                        # Auto-fix lint + format
make test                       # Run tests (pytest)
make sync                       # Run ETL pipeline
make api                        # Start API server
make agent-build                # Build agent sandbox Docker image
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

All secrets are loaded by the `PluginManager` and injected into `PluginContext`. Plugin code can use `os.getenv()` or the `secret()` helper from `shared.plugin_sdk`. The `cli.py` files use `load_dotenv()` for standalone execution.

### Conventions

- `client.py`: NO `load_dotenv()`. Secrets via `os.getenv()` or `secret()`.
- `cli.py`: YES `load_dotenv()` at top (standalone support). Use `from shared.cli_tables import Table` for rich tables.
- Methods starting with `_` or lifecycle methods (`close`, `connect`, `shutdown`) are excluded from tool registration.
- Stub clients (for CLI-only tools) are acceptable but should have docstrings explaining planned capabilities.

## tmux / subprocess rules

- **Never use `sleep`** between `send-keys` and `capture-pane`. Use a tight polling loop with `capture-pane` — the loop itself is the wait mechanism.
- **Never pipe remote commands through `2>&1 | tail` or similar** — this eats useful output. Run commands directly so output streams naturally to the terminal pane.
- When launching Claude CLI subprocesses, always use `--dangerously-skip-permissions` so they don't hang waiting for user input.

## Rules

- All imports must be at the top of the file, never inside functions
- Use absolute package imports everywhere (`from shared.X`, `from api.X`, `from etl.X`), not relative imports (`from .X`, `from ..X`)
- Python 3.11+, use `uv` for all dependency management — never pip/poetry/pipenv
- `ruff` for linting and formatting (line-length=100)
- All secrets via environment variables, never hardcode credentials
- Use `asyncpg` for Postgres connections, `pgvector` for embeddings
- No staging views or mart views — query `raw_records` JSONB directly via `data->>'field'`
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
uv run mypy src/api src/etl src/shared
```

## Deployment

- **Host**: `206.223.235.69` (ssh `ubuntu@206.223.235.69`)
- **Repo on host**: `~/github/paradigmxyz/ai_v2`
- **Orchestration**: `docker compose` (API, ETL, Postgres, Redis, Slackbot)
- **Tunnel**: Cloudflare tunnel (`cloudflared`) → `https://svc-ai.paradigm.xyz/mcp/`
- **MCP URL**: `https://svc-ai.paradigm.xyz/mcp/` (Bearer auth with API_SECRET_KEY)
- **Agent image**: Built separately on host via `docker build -t agent:latest plugins/agent/`

### Deploy steps

```bash
ssh ubuntu@206.223.235.69
cd ~/github/paradigmxyz/ai_v2
git pull --ff-only
docker compose up -d --build api        # Rebuild + restart API
docker build -t agent:latest plugins/agent/  # Rebuild agent image
```

## Running Locally

```bash
# 1. Start Postgres
docker compose up -d postgres

# 2. Start API (loads all plugins, serves MCP at /mcp)
make api
# → http://localhost:8000/mcp (Bearer auth with API_SECRET_KEY)

# 3. Build agent image
make agent-build

# 4. Test agent container directly
source .env
docker run --rm -e AMP_API_KEY="$AMP_API_KEY" -e AI_V2_API_KEY="$API_SECRET_KEY" \
  agent:latest amp --no-ide --no-notifications --dangerously-allow-all -x "hello world"

# 5. Connect from Claude CLI:
#    claude mcp add --scope user --transport http \
#      --header "Authorization: Bearer <API_SECRET_KEY>" \
#      -- tempo-ai "https://svc-ai.paradigm.xyz/mcp/"
```
