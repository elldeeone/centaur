# AI v2 — Dev Instructions

## ⚠️ Production Box — Hands Off

**NEVER SSH into, deploy to, restart, rebuild, or run any command on the production box unless the user explicitly tells you to.** This includes `docker compose`, `systemctl`, `scp`, or any remote command. Always do work locally first and let CI handle deploys, or wait for explicit instruction.

**SSH access**: Always use the IP address: `ssh ubuntu@206.223.235.69`. Never SSH via hostname.

**Remote API calls**: Use the hostname `svc-ai.paradigm.xyz` for all remote API/HTTP calls (e.g., `curl https://svc-ai.paradigm.xyz/...`).

## Architecture Overview

```
                         ┌─────────────────────────────────────────────┐
                         │              nginx (:8000)                  │
                         │  Reverse proxy + auth gate (auth_request)   │
                         │  /, /_next → slackbot | /grafana → grafana │
                         │  /api/*, /agent/*, /tools/* → api          │
                         └──────┬──────────┬──────────┬───────────────┘
                                │          │          │
                    ┌───────────┘          │          └───────────┐
                    ▼                      ▼                      ▼
             ┌────────────┐       ┌──────────────┐       ┌──────────────┐
             │ auth (:4000)│       │  api (:8000)  │       │  slackbot    │
             │ Starlette   │       │  FastAPI      │       │  Next.js     │
             │ HMAC cookie │       │               │       │  (:3001)     │
             └────────────┘       │  routers/     │       └──────────────┘
                                  │  ├ agent.py    │              │
                    ┌──── Slack ──│  ├ slack_events│              │
                    │  webhooks   │  ├ threads.py  │    Thread viewer UI
                    │             │  ├ search.py   │    Postgres-first load
                    │             │  ├ query.py    │    SSE only if running
                    │             │  ├ secrets.py  │
                    │             │  ├ admin.py    │
                    │             │  └ health.py   │
                    │             │                │
                    │             │  agent.py ─── Docker lifecycle │
                    │             │  mcp_server.py ── external MCP │
                    │             └───────┬────────┘
                    │                     │ Docker socket
                    │                     ▼
                    │             ┌──────────────┐       ┌──────────────┐
                    │             │  sandbox/     │──────►│  firewall    │
                    │             │  agent2:latest│ HTTPS │  mitmproxy   │
                    │             │  amp/claude/  │ proxy │  injects     │
                    │             │  codex        │       │  real keys   │
                    │             └──────┬────────┘       └──────┬───────┘
                    │                    │ curl REST              │
                    │                    └──► /tools/* /search    │
                    │                         /query /agent       │
                    │                                             │
                    │             ┌──────────────┐       ┌──────────────┐
                    │             │  secrets      │       │  etl         │
                    │             │  (:8100)      │◄──────│  continuous  │
                    │             │  1Password    │       │  ingest      │
                    │             │  cache        │       └──────────────┘
                    │             └──────────────┘
                    │
                    ▼
               ┌──────────┐
               │ Postgres  │    pgvector, raw_records JSONB
               │ + Redis   │    agent_sessions, agent_turns
               └──────────┘
```

### Network Isolation

- **`secrets_net`** (internal): Only api, etl, firewall, slackbot, auth → secrets
- **`agent_net`** (internal): sandbox containers ↔ firewall ↔ api
- **`default`**: nginx, api, slackbot, auth, grafana, monitoring

### End-to-End Request Flow

1. User mentions bot in Slack → webhook → nginx → api → `slack_events.py`
2. API spawns/reuses Docker container (`agent2:latest`) for that Slack thread
3. Executes harness (amp/claude-code/codex) via `docker exec`
4. Harness calls tools via `curl` back to API at `http://api:8000` (REST, NOT MCP)
5. LLM API calls route through firewall proxy which injects real credentials
6. Results stream as JSON events → SSE to thread viewer UI + posted to Slack

## Directory Structure

```
ai_v2/
├── src/
│   ├── api/              # FastAPI backend (routers/, agent.py, app.py, mcp_server.py)
│   ├── etl/              # Continuous ingest pipelines → Postgres
│   ├── secret_manager/   # 1Password vault cache sidecar (:8100)
│   └── shared/           # Shared utilities, tool_manager.py
├── services/
│   ├── auth/             # Starlette password-session auth sidecar (:4000)
│   └── firewall/         # mitmproxy addon — credential injection proxy
├── apps/
│   └── slackbot/         # Next.js — Bolt event listener + thread viewer UI (pnpm)
├── sandbox/
│   ├── Dockerfile        # Agent container image (Ubuntu 24.04 + uv + gh + node + amp)
│   ├── entrypoint.sh     # Writes harness configs, signals readiness
│   ├── SYSTEM_PROMPT.md  # Baked as ~/AGENTS.md — tells harness to curl the API
│   └── call.sh           # Helper for tool calls from inside the container
├── tools/                # 73 tool integrations (slack, twitter, dune, etherscan, ...)
├── pi-plugins/           # TypeScript plugins (handoff, tool-harness, system-prompt)
├── migrations/           # Alembic migration versions
├── monitoring/           # nginx.conf, Grafana dashboards, Prometheus, Loki, Promtail
├── scripts/              # Operational scripts
├── tests/                # pytest tests
├── docker-compose.yml    # Full stack: 12 services
├── Dockerfile            # API + ETL image
└── entrypoint.sh         # API container entrypoint
```

## Code Conventions

- Python 3.11+, `uv` for deps, `ruff` for lint/format (line-length=100)
- `apps/slackbot` uses `pnpm` only (single lockfile: `pnpm-lock.yaml`)
- All imports at top of file, never inside functions
- Absolute imports only: `from shared.X`, `from api.X`, `from etl.X` — no relative imports
- All secrets via env vars, never hardcode
- **1Password**: Use account `paradigmoperationslp` and vault `Paradigm AI Secrets & API Keys` (ID: `7ycqwxmheirj5zoyqmd27fmbca`). Always pass `--account paradigmoperationslp` to `op` commands.
- `asyncpg` for Postgres, `pgvector` for embeddings
- Query `raw_records` JSONB directly — no staging/mart views
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`

## Lint & Test

```bash
make lint             # ruff check + format --check
make fmt              # auto-fix
make test             # pytest
uv run mypy src/api src/etl src/shared
```

## Tool Conventions

Every tool: `tools/<name>/` with `client.py` (class + `_client()` factory), `pyproject.toml` (`[tool.ai-v2] module = "client.py"`), optional `cli.py`.

- `client.py`: NO `load_dotenv()`. Secrets via `os.getenv()` or `secret()`.
- `cli.py`: YES `load_dotenv()` at top. Thin typer wrapper.
- Methods starting with `_` or lifecycle methods are excluded from registration.
- Secrets resolution: tool `.env` → root `.env` → environment variables.

## Agent Sandbox Architecture

1 Slack thread = 1 Docker container. The API spawns sibling containers (via Docker socket) running harness CLIs (amp, claude-code, codex). Inside the container, the harness calls back to the API via `curl` over the REST API — **NOT MCP**. MCP is only served at `/mcp` for external 3rd-party clients.

Container config:
- Joins `ai_v2_agent_net` Docker network → API reachable at `http://api:8000`
- Entrypoint injects `AI_V2_API_URL` and `AI_V2_API_KEY` env vars
- Stub API keys (e.g., `sk-ant-api03-REDACTED`) so harnesses init in API-key mode (not browser login)
- `HTTPS_PROXY=http://firewall:8080` routes LLM calls through the firewall for credential injection
- Resource limits: 4GB memory, 2 CPUs
- Agent image MUST be tagged `agent2:latest` (not `agent:latest`)
- Labels: `agent2=true`, `ai2.thread`, `ai2.harness` for discovery/recovery

### Credential Injection (Firewall)

Sandbox containers never see real API keys. The firewall (`services/firewall/addon.py`) intercepts HTTPS and injects credentials from the secrets service:

| Target host | Header | Format |
|-------------|--------|--------|
| `api.anthropic.com` | `x-api-key` | raw |
| `api.openai.com` | `authorization` | bearer |
| `ampcode.com` | `authorization` | bearer |
| `api.github.com` | `authorization` | token |
| `github.com` | `authorization` | basic auth |

### Session Persistence

- **`agent_sessions`** table: tracks container ID, harness, state, thread key
- **`agent_turns`** table: tracks per-turn user message, events JSONB, result, timing
- On API restart: `recover_sessions()` reconciles Postgres state with live Docker containers
- Containers discoverable via Docker labels even if DB is out of sync

## Thread Viewer (apps/slackbot)

Next.js app serving the agent conversation UI:
- Root `/` = thread list, `/[id]` = thread detail (route group `(threads)`)
- **Postgres-first loading**: historical data rendered immediately from `agent_turns`, SSE connected only if thread is `running`/`working`
- Uses `ai-elements` component library: `Conversation`, `Message`, `Reasoning`, `Terminal`, `StepGroup`
- `@tanstack/react-virtual` for sidebar virtualization, `StickToBottom` for chat scroll
- Avatar gutter pattern: absolute positioned left of message boxes
- `suppressHydrationWarning` on body for browser extension compatibility

## Deployment — CI/CD (preferred)

All deploys happen automatically via GitHub Actions on merge to `main`. **Never SSH to deploy** — just push to main and the self-hosted runner on `svc-ai.paradigm.xyz` handles it.

| Change | Deploy action |
|--------|--------------|
| `tools/**` only | Zero-downtime hot-reload (file watcher auto-detects, no restart) |
| `src/**` | `docker compose up -d --build api` |
| `src/etl/` or `src/shared/` | `docker compose up -d --build etl` |
| `apps/slackbot/**` | `docker compose up -d --build slackbot` |
| `sandbox/**` | `docker build -t agent2:latest sandbox/` |
| `Dockerfile`, `pyproject.toml`, `uv.lock`, `docker-compose.yml`, `migrations/` | Rebuild API + ETL |

**Tool hot-reload:** The API watches the bind-mounted `tools/` directory via `watchfiles`. When `git pull` updates tool files, the API auto-reloads within seconds — no container restart needed.

**Admin endpoint:** `POST /admin/reload-tools` is available as a manual fallback.

## Secret Manager

**NEVER manually restart or redeploy the `secrets` container.** It requires `OP_SERVICE_ACCOUNT_TOKEN` which is only injected by CI (GitHub Actions secret). Manual `docker compose up -d secrets` will start it without the token, breaking all secret resolution across the stack. Always let CI handle secrets container deploys.

The secrets service (`src/secret_manager/app.py`) loads all secrets from the 1Password vault on startup and refreshes every 5 minutes. 1Password item titles are normalized to ENV_VAR style (e.g., "Claude API" → `ANTHROPIC_API_KEY`).

## Security Model

- **API auth**: Bearer token via `verify_api_key` dependency; Docker bridge IPs bypass auth for container→API calls
- **Slack**: HMAC-SHA256 signature verification on all webhooks
- **UI**: Password-based HMAC session cookie (`paradigm_ui_session`); nginx `auth_request` gates all UI routes
- **Sandbox isolation**: Containers get stub keys only; real keys injected by firewall proxy in-flight
- **Filesystem**: Host `~/github` mounted read-only by default; only working repo is read-write

## E2E Testing (without Slack)

### 1. Bring up the stack

```bash
docker compose up -d postgres api    # API + Postgres
docker build -t agent2:latest sandbox/  # rebuild agent image after sandbox/ changes
source .env
```

### 2. Execute a message (auto-spawns container)

```bash
curl -s -X POST http://localhost:8000/agent/execute \
  -H "Authorization: Bearer $API_SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "slack_thread_key": "test:e2e-1",
    "message": "find me the last 10 messages in the #investing slack channel",
    "harness": "amp"
  }'
```

### 3. Follow-up (same container, same session)

```bash
curl -s -X POST http://localhost:8000/agent/execute \
  -H "Authorization: Bearer $API_SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "slack_thread_key": "test:e2e-1",
    "message": "now summarize the key topics"
  }'
```

### 4. Inspect / Clean up

```bash
curl -s "http://localhost:8000/agent/status?key=test:e2e-1" \
  -H "Authorization: Bearer $API_SECRET_KEY" | jq

curl -s -X POST http://localhost:8000/agent/stop \
  -H "Authorization: Bearer $API_SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{"slack_thread_key": "test:e2e-1"}'
```

### Debugging connectivity

```bash
docker ps --filter label=agent2=true
docker exec <container_id> curl -s -H "Authorization: Bearer $AI_V2_API_KEY" http://api:8000/health
```

### Container cleanup

Only touch `agent2` containers (label `agent2=true`). **Never stop or remove `agent-*` (v1) containers** — those are managed separately.

```bash
# List agent2 containers
docker ps --filter label=agent2=true

# Stop and prune all agent2 containers
docker ps --filter label=agent2=true -q | xargs -r docker stop
docker container prune -f --filter label=agent2=true
```

