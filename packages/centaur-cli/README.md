# Centaur CLI

`centaur` is the agent-readable setup CLI for Centaur. It is built with
[`incur`](https://github.com/wevm/incur), so agents can inspect it with
`--llms`, use TOON/JSON output, and register it as an MCP server when useful.
It scaffolds an overlay, records resumable onboarding state, generates
integration templates, validates local prerequisites, and prints exact repair
steps for Slack, model, GitHub, secrets, and deployment setup.

```bash
pnpm --silent --filter @centaur/cli centaur init --org acme --assistant-name centaur --domain centaur.acme.com --harness codex --auth-mode api_key
pnpm --silent --filter @centaur/cli centaur integrations slack-manifest --domain centaur.acme.com --app-name centaur --output org/slack-app-manifest.json --copy --harness codex --auth-mode api_key
pnpm --silent --filter @centaur/cli centaur secrets collect --backend local-env --install-mode local --harness codex --auth-mode api_key --overlay-path org
pnpm --silent --filter @centaur/cli centaur doctor --deep --harness codex --auth-mode api_key --secret-backend local-env --install-mode local
pnpm --silent --filter @centaur/cli centaur deploy k3s
pnpm --silent --filter @centaur/cli centaur run "Reply with exactly PONG and nothing else." --thread cli:test --harness codex
```

`centaur init` returns CTAs for the next one-off commands, so an agent can keep
driving setup without guessing. Choose exactly one default harness per
deployment: `--harness codex` or `--harness claude-code`. Use
`--auth-mode access_token` for the selected harness when routing through a
dedicated ChatGPT or Claude.ai subscription account.

`integrations slack-manifest --copy` copies the Slack app manifest JSON to the
clipboard so you can alt-tab into Slack and paste it. `secrets collect` prompts
for required values with masked input, runs the selected Codex or Claude Code
login command when subscription auth is selected, and writes the collected
values into the chosen secret backend.

`centaur run` drives the durable agent API directly: it spawns or reuses a
thread runtime, persists the user message, enqueues execution, pipes every SSE
event as a structured chunk, and reads final execution state. It does not
dedupe or repair stream events; use `--format jsonl` when an agent needs exact
event-by-event output. Set `CENTAUR_API_URL` and `CENTAUR_API_KEY`, or pass
`--api-url` and `--api-key`.
