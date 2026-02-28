# Tempo AI v2

Rebuild of the Tempo AI system: **Postgres+pgvector** for data + search, **FastAPI+MCP** API layer, **Codex+Docker** sandbox.

## Architecture

```
                    ┌──────────────────────────────┐
                    │         Remote Clients        │
                    │  (Codex, agents, MCP clients) │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │     api (FastAPI+MCP)         │
                    │  /api/search  (hybrid search) │
                    │  /api/query   (JSONB queries) │
                    │  /api/search/sql (raw SQL)    │
                    │  /mcp         (core + dynamic │
                    │                tools)       │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │   Postgres 16 + pgvector      │
                    │                               │
                    │  raw_records  (JSONB, all src) │
                    │  embeddings   (vector(1536))   │
                    │  people / entity_mappings      │
                    │  sync_cursors / sync_runs      │
                    │  secrets                       │
                    └──────────────▲───────────────┘
                                   │
                    ┌──────────────┴───────────────┐
                    │   ETL pipeline (10 extractors) │
                    │   → raw_records → embeddings   │
                    └──────────────────────────────┘
```

**No staging views. No mart views.** All queries hit JSONB directly (`data->>'field'`) or the `embeddings` table for semantic search.

### Extractors (company knowledge base only)

Slack, Linear, GitHub, GCal, Gmail, GDrive, Granola, Attio, Pylon, BetterStack.

External tools (allium, defillama, etc.) are called on-demand — not ingested.

### API Layer

- **`POST /api/search`** — Hybrid vector + FTS search with RRF ranking
- **`POST /api/search/sql`** — Run read-only SQL directly against `raw_records` JSONB
- **`GET /api/query/*`** — Structured endpoints (slack/messages, linear/issues, github/prs, timeline, people)
- **`/mcp`** — core MCP tools plus dynamically discovered in-process tools from `tools/`

### Sandbox

Docker image preloaded with 28 tempoxyz repos, auto-updated every 6h. For Codex / agent code execution.

## Quick Start

```bash
gh repo clone tempoxyz/ai_v2
cd ai_v2
cp .env.example .env        # fill in secrets
docker compose up -d         # start Postgres
make install                 # install Python deps
make migrate                 # run Alembic migrations
make sync                    # run ETL pipeline
make api                     # start API server
```

## CLI

```bash
ai-v2 sync                     # Run ETL pipeline
ai-v2 serve                    # Start API server
ai-v2 embed                    # Generate embeddings
ai-v2 status                   # Show sync status
ai-v2 search "query"           # Test hybrid search
ai-v2 continuous               # Run continuous sync loop
ai-v2 migrate-from-sqlite PATH # Import from metronome SQLite
ai-v2 tools list             # Show loaded tools + discovered tools
ai-v2 tools run TOOL [ARGS]  # Run a tool CLI by tool name/alias
ai-v2 tools test             # Smoke-test tool imports, discovery, and CLI wiring
ai-v2 sandbox sync-repos       # Clone/update repos
ai-v2 sandbox build            # Build sandbox Docker image
ai-v2 sandbox update           # Sync + rebuild image
ai-v2 sandbox run              # Run sandbox container
ai-v2 sandbox cron             # Continuous repo sync loop
```

## Deployment (dev-aibot)

```bash
bash scripts/setup-postgres.sh
DATABASE_URL=... uv run alembic -c migrations/alembic.ini upgrade head
python scripts/migrate-from-sqlite.py --sqlite-path ~/.pov/pov.db --database-url ...
python scripts/generate-embeddings.py --database-url ... --openai-api-key ...
bash scripts/deploy-dev-aibot.sh
```

## Project Structure

```
ai_v2/
├── src/ai_v2/          # Single Python package
│   ├── extractors/     # 10 source extractors
│   ├── routers/        # FastAPI route handlers
│   ├── sandbox/        # Docker sandbox builder
│   ├── app.py          # FastAPI application
│   ├── cli.py          # Unified CLI
│   ├── config.py       # All settings
│   ├── db.py           # Postgres pool + schema
│   ├── deps.py         # FastAPI dependencies
│   ├── embeddings.py   # pgvector embedding service
│   ├── mcp_server.py   # MCP server (7 tools)
│   ├── models.py       # Pydantic models
│   ├── cursors.py      # Sync cursor management
│   └── pipeline.py     # ETL pipeline orchestrator
├── migrations/         # Alembic (1 migration: core schema)
├── sandbox/            # Dockerfile + entrypoint
├── scripts/            # setup-postgres, migrate, deploy, embeddings
├── docker-compose.yml
├── Makefile
└── pyproject.toml
```
