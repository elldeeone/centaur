---
name: centaur-builder
description: "Hackathon onboarding and building guide for Centaur. Use when someone asks about the hackathon, wants to build a tool/skill/workflow/web app on Centaur, or needs help deploying their hackathon project. Covers API setup, building patterns, and deployment."
---

# Paradigm Hackathon

Guide for building and deploying things on Centaur during the hackathon. No engineering background required.

## Setup

1. **Get your API key** from the [hackathon spreadsheet](https://docs.google.com/spreadsheets/d/15Zu4OVXYq640YCeQouLOIqz-IZTdEYF1NomLpF0HUuI). It looks like `aiv2_...`
2. **Set it as an env var**: `export CENTAUR_API_KEY="aiv2_your_key_here"`
3. **Verify it works**:

```bash
curl -s https://svc-ai.dayno.xyz/health -H "X-Api-Key: $CENTAUR_API_KEY"
```

**Base URL**: `https://svc-ai.dayno.xyz`
**Auth header**: `X-Api-Key: aiv2_...` or `Authorization: Bearer aiv2_...`

## What Can Be Built

| Type | Where it lives | Tutorial | Description |
|------|---------------|----------|-------------|
| **Tool** | `tools/<name>/` | `reference/tutorial-tool.md` | Connects Centaur to an external API. Auto-discovered, hot-reloaded. |
| **Skill** | `.agents/skills/<name>/` | `reference/tutorial-skill.md` | A markdown recipe that teaches Centaur how to do a multi-step task. |
| **Workflow** | `workflows/<name>.py` | `reference/tutorial-workflow.md` | A durable, scheduled, or multi-step Python automation. Hot-reloaded. |
| **Web App** | Deployed via `POST /apps` | `reference/tutorial-webapp.md` | A frontend (Next.js, Python, anything) running on Centaur infra. |

Each tutorial covers the full loop: **build locally → test locally → deploy → verify**. Read the relevant tutorial file for step-by-step instructions.

## Harness Engineering Setup

If someone asks how to set up a repo or skill for harness engineering, do not stop at scaffolding, linting, and CI. In Centaur, harness engineering means **repo bootstrap plus verification loops**: tests, regression cases, evals, benchmarks, smoke checks, and observability.

Treat the repo as the shell around a feedback loop:

- **Bootstrap**: repo layout, dependencies, env setup, formatter, linter, CI
- **Tests**: unit/integration coverage for the core harness path
- **Regression cases**: fixed examples for known failures so they stay fixed
- **Evals**: scored prompts/tasks that measure answer quality over time
- **Benchmarks**: latency, token cost, throughput, or success-rate baselines
- **Smoke checks**: one-command end-to-end verification for a fresh deploy
- **Observability**: logs, traces, and metrics that explain failures in production

### Minimal answer template

Use this structure for first-pass answers about harness-engineering setup:

1. **Repo bootstrap**: create the repo/app skeleton, dependency management, linting, formatting, and CI.
2. **Verification loop**: define the smallest useful test suite, regression fixtures, eval set, and smoke check.
3. **Measurement**: decide what to benchmark and what success metrics to track.
4. **Observability**: wire logs, traces, and dashboards before broad rollout.
5. **Ship order**: bootstrap first, then tests/evals, then benchmarks and observability.

If the user is building a skill, workflow, or tool for harness engineering, answer in that order so the recommendation covers both setup and ongoing quality measurement.

## Helper Script

This skill includes `scripts/centaur-run.sh` — a one-shot wrapper around the spawn → message → execute API flow.

```bash
export CENTAUR_API_KEY="aiv2_..."
scripts/centaur-run.sh "What tools do you have access to?"
```

Use it to quickly talk to a Centaur agent from the terminal without managing thread keys yourself.

## Building a Tool

A tool wraps an external API. Public methods become REST endpoints at `/tools/<name>/<method>`.

### File structure

```
tools/<name>/
├── __init__.py        # Empty
├── client.py          # API client class + _client() factory
├── pyproject.toml     # Metadata + [tool.ai-v2] section
└── .env.example       # Document required API keys (if any)
```

### Minimal tool (public API, no auth)

```python
# tools/hackernews/client.py
import httpx


class HackerNewsClient:
    """Hacker News API client."""

    def top_stories(self, limit: int = 10) -> list[dict]:
        """Get the current top stories from Hacker News."""
        with httpx.Client() as client:
            ids = client.get("https://hacker-news.firebaseio.com/v0/topstories.json").json()
            stories = []
            for story_id in ids[:limit]:
                story = client.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
                ).json()
                stories.append({
                    "title": story.get("title"),
                    "url": story.get("url"),
                    "score": story.get("score"),
                })
            return stories

    def get_item(self, item_id: int) -> dict:
        """Get a single item (story, comment, etc.) by ID."""
        with httpx.Client() as client:
            return client.get(
                f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
            ).json()


def _client() -> HackerNewsClient:
    return HackerNewsClient()
```

```toml
# tools/hackernews/pyproject.toml
[project]
name = "hackernews"
description = "Hacker News API - top stories, items, and comments"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["httpx>=0.27.0"]

[tool.ai-v2]
module = "client.py"
```

### Tool with API key

Use `secret()` from the SDK — never hardcode keys:

```python
# tools/my-api/client.py
import httpx
from centaur_sdk.tool_sdk import secret


class MyApiClient:
    def search(self, query: str, limit: int = 10) -> dict:
        """Search for things."""
        api_key = secret("MY_API_KEY")
        resp = httpx.get(
            "https://api.example.com/search",
            params={"q": query, "limit": limit},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        return resp.json()


def _client() -> MyApiClient:
    return MyApiClient()
```

### Adding your API key to 1Password

If your tool requires an API key, you **must** add it to 1Password so the deployed Centaur instance can access it. Centaur's secrets manager loads all secrets from a shared 1Password vault at startup.

1. Open 1Password and go to the **Paradigm AI Secrets & API Keys** vault
2. Create a new item
3. **Title**: use the exact env var name your code expects (e.g., `MY_API_KEY`, `HACKERNEWS_API_KEY`)
4. **Field**: put the key value in the `password` or `credential` field
5. Save — Centaur refreshes secrets periodically, so it will be available shortly

The title gets normalized to `ENV_VAR_STYLE` automatically (e.g., "My API Key" → `MY_API_KEY`), but it's cleanest to just name it exactly as the env var.

### Tool rules

- `client.py` must have a `_client()` factory function at the bottom
- Public methods → auto-registered as REST endpoints
- Methods starting with `_` → private, not exposed
- Docstrings matter — they become the tool description the agent sees
- Use `secret("KEY_NAME")` for API keys — never hardcode, never use `os.getenv()`
- The key must exist in 1Password (see above) for it to work on the deployed instance
- Add dependencies to `pyproject.toml` under `[project] dependencies`

### Testing a tool via the API

```bash
# Discover a tool's methods
curl -s https://svc-ai.dayno.xyz/tools/<name> \
  -H "X-Api-Key: $CENTAUR_API_KEY"

# Call a method
curl -s -X POST https://svc-ai.dayno.xyz/tools/<name>/<method> \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $CENTAUR_API_KEY" \
  -d '{"query": "test"}'
```

## Building a Skill

A skill is a markdown file with instructions — a recipe card that teaches Centaur how to do something.

### File structure

```
.agents/skills/<name>/
├── SKILL.md           # Instructions (required)
└── scripts/           # Optional helper scripts
    └── do_thing.py
```

### Example

```markdown
---
name: my-cool-skill
description: "Does the cool thing. Use when asked to do the cool thing."
---

# My Cool Skill

## When To Use

Use this skill when the user asks to "do the cool thing" or "run cool analysis."

## Steps

1. Gather the input from the user
2. Call the websearch tool: `call websearch search '{"query": "..."}'`
3. If the result is a comparison or trend across ≥ 3 items, render a chart
   image via `call chart render_chart` and upload via
   `slack upload_file` with `alt_text`. If the user needs exact lookup values,
   use a compact text/code-block table instead. See the `charting` skill for
   the brief-first contract.
4. Beneath the chart, summarize the top 1-2 findings in plain prose

## Output Format

If the data is comparative or has trends, output a chart image (PNG via
`chart render_chart`). If it's ≤ 5 precise values or a single number, output a
sentence or KPI tile. **Markdown tables of >2 rows render badly on Slack
mobile — avoid unless the user explicitly wants a sortable table.**
```

### Skill rules

- Frontmatter `name` must match the directory name
- `description` should say what it does AND when to use it
- Keep SKILL.md under 500 lines — split large content into `reference/` files
- Reference scripts with execution intent: "Run `scripts/validate.py` to check..."
- **Charts beat tables on Slack mobile.** When your skill compares > 2 items
  or shows a trend, ship a PNG via `call chart render_chart` (the
  charting skill handles the brief-first + verify loop). Reserve markdown
  tables for exact-value lookups where the user has explicitly asked.

### Skills for harness engineering

When the user wants a skill that helps set up repos for harness engineering, make the skill cover the verification loop explicitly. A good harness-engineering skill should tell the agent to:

1. scaffold the repo or integration surface,
2. add high-signal tests and regression cases,
3. define a small eval or benchmark harness,
4. add a smoke check for deploy confidence, and
5. point to the observability signals that confirm the harness works.

That prevents answers from collapsing into "create the repo and add CI" when the real need is ongoing quality measurement.

## Building a Workflow

A workflow is a durable Python automation that can sleep, retry, run agents, and survive crashes.

### File structure

A single Python file in `workflows/`:

```python
# workflows/morning_briefing.py
"""Workflow: morning briefing that summarizes news."""

from dataclasses import dataclass
from typing import Any

from api.workflow_engine import WorkflowContext

WORKFLOW_NAME = "morning_briefing"


@dataclass
class Input:
    topic: str = "crypto"
    slack_channel: str = ""


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    result = await ctx.run_agent(
        "research",
        text=f"Search for the latest news about {inp.topic} and summarize the top 5 stories.",
    )
    return {"topic": inp.topic, "result": result}
```

### Workflow building blocks

| Primitive | What it does |
|-----------|-------------|
| `ctx.step(name, fn)` | Run `fn` exactly once; cached on replay |
| `ctx.sleep(name, duration)` | Suspend for a duration |
| `ctx.run_agent(name, text=...)` | Run a full agent turn and wait for the result |
| `ctx.start_agent(name, text=...)` | Fire-and-forget agent turn |
| `ctx.wait_for_event(name, event_type, correlation_id)` | Wait for an external event |
| `ctx.run_workflow(name, workflow_name, input)` | Run a child workflow |

### Triggering a workflow

```bash
curl -s -X POST https://svc-ai.dayno.xyz/workflows/runs \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $CENTAUR_API_KEY" \
  -d '{"workflow_name": "morning_briefing", "input": {"topic": "ethereum"}}'
```

### Checking workflow status

```bash
curl -s https://svc-ai.dayno.xyz/workflows/runs/<run_id> \
  -H "X-Api-Key: $CENTAUR_API_KEY"
```

## Building a Web App

Deploy any web app (Next.js, Python, static site) on Centaur infrastructure.

### Templates

Fork one of these to get started:

- **Next.js**: `paradigmxyz/centaur-template-nextjs`
- **Cloudflare Workers**: `paradigmxyz/centaur-template-cloudflare`

### Deploy

```bash
curl -s -X POST https://svc-ai.dayno.xyz/apps \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $CENTAUR_API_KEY" \
  -d '{
    "name": "my-dashboard",
    "repo_url": "https://github.com/myname/my-app",
    "port": 3000
  }'
```

Live at: `https://my-dashboard.svc-ai.dayno.xyz`

### App options

| Field | Description |
|-------|-------------|
| `name` | URL-safe slug (lowercase, hyphens ok) |
| `repo_url` | GitHub repo to clone and build |
| `port` | Port your app listens on |
| `build_cmd` | Custom build command (default: `npm install && npm run build`) |
| `start_cmd` | Custom start command (default: `npm start`) |
| `env` | JSON object of env vars to inject |
| `basic_auth_user` / `basic_auth_pass` | Optional password protection |

### Managing apps

```bash
# List all apps
curl -s https://svc-ai.dayno.xyz/apps -H "X-Api-Key: $CENTAUR_API_KEY"

# Check app status and build logs
curl -s https://svc-ai.dayno.xyz/apps/<name> -H "X-Api-Key: $CENTAUR_API_KEY"

# Restart (rebuild from latest git)
curl -s -X POST https://svc-ai.dayno.xyz/apps/<name>/restart -H "X-Api-Key: $CENTAUR_API_KEY"

# Delete
curl -s -X DELETE https://svc-ai.dayno.xyz/apps/<name> -H "X-Api-Key: $CENTAUR_API_KEY"
```

## Deploying to Centaur (Tools, Skills, Workflows)

Tools, skills, and workflows go live by landing on the `main` branch of `paradigmxyz/centaur`. They **hot-reload within seconds** — no restart needed.

**You do NOT need a GitHub account.** Use the Centaur API to trigger an agent that handles the git flow with local validation and PR gates.

### How to deploy

Use the `spawn → message → execute` API to tell a Centaur agent what to add. The agent runs in a sandbox with full `git` and `gh` CLI access.

```bash
export CENTAUR_API_KEY="aiv2_your_key_here"
THREAD_KEY="deploy-$(date +%s)"

# 1. Spawn
SPAWN=$(curl -s -X POST https://svc-ai.dayno.xyz/agent/spawn \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $CENTAUR_API_KEY" \
  -d "{\"thread_key\":\"${THREAD_KEY}\",\"harness\":\"amp\"}")
AG=$(echo "$SPAWN" | python3 -c "import sys,json; print(json.load(sys.stdin)['assignment_generation'])")

# 2. Message — describe what to add (paste code, or describe it)
curl -s -X POST https://svc-ai.dayno.xyz/agent/message \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $CENTAUR_API_KEY" \
  -d "{\"thread_key\":\"${THREAD_KEY}\",\"assignment_generation\":${AG},\"role\":\"user\",\"parts\":[{\"type\":\"text\",\"text\":\"Add a new tool called hackernews to tools/hackernews/ in paradigmxyz/centaur. Here is client.py: ... Create the pyproject.toml too. Run the smallest relevant local validation, then open a PR.\"}]}"

# 3. Execute
EXECUTE=$(curl -s -X POST https://svc-ai.dayno.xyz/agent/execute \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $CENTAUR_API_KEY" \
  -d "{\"thread_key\":\"${THREAD_KEY}\",\"assignment_generation\":${AG},\"harness\":\"amp\",\"delivery\":{\"platform\":\"dev\"}}")
EXECUTION_ID=$(echo "$EXECUTE" | python3 -c "import sys,json; print(json.load(sys.stdin)['execution_id'])")

# 4. Stream progress
curl -s -N "https://svc-ai.dayno.xyz/agent/threads/${THREAD_KEY}/events?execution_id=${EXECUTION_ID}&after_event_id=0" \
  -H "X-Api-Key: $CENTAUR_API_KEY"
```

Or use the bundled helper: `scripts/centaur-run.sh "Add a tool called hackernews to paradigmxyz/centaur. <code>. Validate locally and open a PR."`

### What the agent does

1. `git-branch paradigmxyz/centaur` → writable clone
2. Writes your files to the correct directory
3. Inspects status and performs only the next missing shipping step: local validation → commit → push → PR
4. Merges or deploys only after a PR exists, CI is green, and the user explicitly asked to merge or deploy
5. After merge, tools, skills, and workflows hot-reload within seconds

### Explicit PR / deploy follow-ups

If the user follows up with "open the PR", "ship it", "merge this", "deploy it", or "set that up as a PR and deploy", treat that as a request to execute the next shipping step, not as a request to restate the build work, only when the phrase clearly refers to the same-thread/current-artifact Centaur build context. If the artifact or PR is ambiguous, ask which branch, PR, app, tool, skill, or workflow they mean.

1. Start with the current ship status: say whether a branch, commit, PR, CI result, merge, or deploy already exists.
2. Inspect the repo state before acting. If the work only exists in a read-only checkout, run `git-branch paradigmxyz/centaur`; if there is no diff to commit, say that explicitly.
3. Perform only the next missing shipping step for the requested outcome. Do not skip ahead: validate before the first push, push before opening a PR, and open a PR before any merge or deploy.
4. Before pushing, run the smallest relevant local validation for the changed artifact and report it:
   - tool: import or targeted method check, plus any narrow test if one exists
   - skill: frontmatter/Markdown sanity check and re-read the changed instructions
   - workflow: syntax/import check or targeted workflow test
   - web app: build or local smoke check for the touched route
5. For "merge this": if an existing PR is already in context, inspect that PR and its checks; if no PR exists yet, create or open the PR as the next step and report that merge is gated on CI.
6. Merge or deploy only when all three gates are true: the PR exists, CI is green, and the user explicitly asked to merge or deploy. If CI is pending or failing, stop and report the status.
7. If the user explicitly asked to deploy and the gates pass, do the deploy step that matches the artifact:
   - tool / skill / workflow: merge the PR so `main` hot-reloads it
   - web app: create or restart the app and report the resulting URL or status
8. If any step is blocked, stop at that exact step and name the blocker plainly: no writable clone, no diff to commit, missing GitHub permission, failing local validation, missing deploy credential, pending/failing CI, or unsupported deploy surface.
9. Do not answer a shipping follow-up by summarizing the implementation that already happened.

Treat only explicit ship-now requests in the current Centaur build context as execution requests. If the user asks "how do I deploy this?", "what would the PR look like?", or "can you explain the rollout?", answer with guidance instead of taking action.

### Deploy checklist

- [ ] Tool has `client.py` with `_client()` factory and `pyproject.toml` with `[tool.ai-v2]`
- [ ] Skill has `SKILL.md` with valid frontmatter (`name`, `description`)
- [ ] Workflow has `WORKFLOW_NAME` and `async def handler(inp, ctx)`
- [ ] No hardcoded secrets — use `secret("KEY")` for API keys

## Quick Reference

| I want to... | Do this |
|--------------|---------|
| Verify my API key | `curl -s https://svc-ai.dayno.xyz/health -H "X-Api-Key: $CENTAUR_API_KEY"` |
| List all tools | `curl -s https://svc-ai.dayno.xyz/tools -H "X-Api-Key: $CENTAUR_API_KEY"` |
| Discover a tool's methods | `GET /tools/<name>` |
| Call a tool | `POST /tools/<name>/<method>` with JSON body |
| Run an agent | `POST /agent/spawn` → `POST /agent/message` → `POST /agent/execute` |
| Stream agent output | `GET /agent/threads/<key>/events?execution_id=<id>&after_event_id=0` |
| Deploy a web app | `POST /apps` with `name`, `repo_url`, `port` |
| Start a workflow | `POST /workflows/runs` with `workflow_name`, `input` |
| Check workflow status | `GET /workflows/runs/<run_id>` |
| Deploy code (tool/skill/workflow) | Trigger an agent via the API and tell it to validate locally and open a PR; explicitly ask it to merge only after CI is green |

## FAQ

**Do I need a GitHub account?**
No. Trigger a Centaur agent via the API and tell it to validate locally and open a PR. If you want it merged, explicitly ask after the PR exists and CI is green.

**Can I build without an API key for the external service?**
Yes — many APIs are free/public (Hacker News, DeFi Llama, CoinDesk, Google News, Polymarket). For paid APIs, bring your own key or ask in `#hackathon2026`.

**Can I use Python libraries?**
Yes. Add them to `pyproject.toml` under `dependencies`. They install automatically.

**What if I break something?**
Tools, skills, and workflows run in isolation, and PRs give us a review/CI checkpoint before anything lands on `main`.

**How do I see what Centaur already has?**
Hit `GET /tools` to see 60+ tools. Check `.agents/skills/` for existing skills. Check `workflows/` for existing workflows.
