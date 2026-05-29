---
title: Quickstart
description: Boot Centaur locally and verify the control plane.
---

# Quickstart

This guide gets you from a fresh checkout to a working local Centaur stack. You
do not need a full production Kubernetes installation for local setup: a
lightweight k3s-based cluster is enough. The happy path is: point `kubectl` at
that cluster, bootstrap the required infra Secret, run `just up`, verify the
API, then run one agent turn without Slack. If you want the easiest small-host
path first, start with [Running Centaur on a Mac Mini-style
setup](/mac-mini-setup).

If you want an agent to drive setup with you, point it at these docs: every page
is available through `/llms.txt`, `/llms-full.txt`, and static Markdown files
such as `/md/quickstart.md`.

## 1. Install prerequisites

For agent-driven setup, install the onboarding CLI first. The commands return
structured output and CTAs so an agent can keep driving the next step:

```bash
curl -fsSL https://centaur.run/install.sh | bash
centaur --llms
centaur setup --org acme --assistant-name centaur --domain centaur.example.com --backend local-env --install-mode local --harness codex --auth-mode api_key --json
centaur init --install-mode local --image-source ghcr --harness codex --auth-mode api_key
centaur integrations slack-manifest --domain centaur.example.com --app-name centaur --output org/slack-app-manifest.json --copy --socket-mode --install-mode local --image-source ghcr --harness codex --auth-mode api_key
centaur secrets collect --backend local-env --install-mode local --image-source ghcr --harness codex --auth-mode api_key --overlay-path org
centaur doctor --deep --harness codex --auth-mode api_key --secret-backend local-env --install-mode local --image-source ghcr
centaur deploy k3s --apply --image-source ghcr --wait --timeout 10m --secrets-file org/secrets.local.env
centaur run "Reply with exactly PONG and nothing else." --local --harness codex --expect PONG --release-thread
centaur slackbot smoke
```

Pick one default harness for the deployment: `codex` or `claude-code`. Use
`--auth-mode access_token` for the selected harness when routing through a
dedicated ChatGPT or Claude.ai subscription account. The Slack manifest command
copies JSON to your clipboard for paste-in-place setup, and `secrets collect`
prompts for secret values with masked input before writing them to the selected
backend. `centaur deploy ... --apply` creates the Kubernetes Secret from the
local secrets file when needed and runs Helm with published
`ghcr.io/paradigmxyz/centaur/*` images, so fresh installs do not need a local
Docker build. The deploy step waits for Kubernetes readiness before the next
command runs. `centaur run --local` verifies a real durable agent turn through
the API pod without requiring a port-forward or external API key.
`centaur slackbot smoke` then sends a signed synthetic Slack mention through
the deployed Slackbot pod and waits for the resulting Slack workflow execution
to complete. After both checks pass, send a real mention in a test Slack
channel to verify workspace delivery.

From the repo root:

```bash
brew install just kubectl helm jq
```

You also need Docker and a local Kubernetes cluster. This can be lightweight:
k3s works on a small VPS, DigitalOcean droplet, Linux box, or Mac Mini-style
host. Docker Desktop with Kubernetes enabled, kind, and minikube are also fine
as long as `kubectl` points at that local cluster and it can run the Helm chart.
The [Mac Mini-style setup guide](/mac-mini-setup) walks through the k3s path.

Check the target before booting Centaur:

```bash
kubectl config current-context
kubectl get nodes
```

The `Justfile` builds local images named `centaur-api:latest`,
`centaur-iron-proxy:latest`, `centaur-slackbot:latest`, and
`centaur-agent:latest`, then deploys `contrib/chart` with
`contrib/chart/values.dev.yaml`.

## 2. Export bootstrap secrets

The default local chart expects one infra Secret named `centaur-infra-env`.
`just bootstrap-secrets` creates it from your shell environment.

`just bootstrap-secrets` currently requires these shell variables:

```bash
export OP_SERVICE_ACCOUNT_TOKEN=...
export OP_VAULT=...
export SLACK_BOT_TOKEN=...
export SLACK_SIGNING_SECRET=...
export SLACKBOT_API_KEY=...
```

Create the Slackbot app at [api.slack.com/apps](https://api.slack.com/apps).
Use the app's Bot User OAuth Token for `SLACK_BOT_TOKEN` and its Signing Secret
for `SLACK_SIGNING_SECRET`.

`OP_SERVICE_ACCOUNT_TOKEN` and `OP_VAULT` let [iron-proxy](https://docs.iron.sh)
resolve model and tool credentials through 1Password. `SLACK_SIGNING_SECRET`
and `SLACKBOT_API_KEY` are API boot requirements in the current chart.
`SLACK_BOT_TOKEN` is required by the default local bootstrap because Slackbot is
enabled in `values.dev.yaml`; use a real token if you want to test Slack.

`SLACKBOT_API_KEY` is a static service token. The API bootstraps that value into
Postgres on startup, so it must exist before `just up`.

Application-level model and tool secrets, such as `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `AMP_API_KEY`, and `GITHUB_TOKEN`, should live in
1Password or the configured [iron-proxy](https://docs.iron.sh) secret source. Sandboxes receive
placeholder values and [iron-proxy](https://docs.iron.sh) injects the real credentials only on approved
outbound requests.

The default harness is `codex`, so `OPENAI_API_KEY` must exist in the configured
secret source before Slack agent turns can complete. Use explicit harness
selectors only when you want a non-default harness such as Amp or Claude Code.

## 3. Boot the stack

```bash
just up
```

That runs:

1. `just bootstrap-secrets`
2. `just build`
3. `just deploy`

Check the namespace:

```bash
just status
```

## 4. Verify the API

The API exposes localhost inside its own deployment. Localhost bypasses external
API-key auth, which is why the health check runs through `kubectl exec`:

```bash
kubectl exec -n centaur deploy/centaur-centaur-api -- \
  curl -fsS http://localhost:8000/health
```

Expected shape:

```json
{"status":"ok"}
```

## 5. Run an agent turn

Before testing Slack, run a local CLI turn. It uses the same durable agent API
that Slackbot uses: spawn or reuse a runtime, persist a message, enqueue an
execution, and poll the execution state until the result contains `PONG`.

```bash
centaur run "Reply with exactly PONG and nothing else." --local --harness codex --expect PONG --release-thread
```

The successful result includes the terminal execution row. The important fields
are:

```json
{
  "status": "completed",
  "result_text": "...PONG..."
}
```

If the smoke test times out or fails, start with the local stack state:

```bash
just status
just logs api
kubectl get pods -n centaur -l centaur.ai/managed=true
```

If you changed the namespace or release name, pass `--namespace` and `--release`
so the CLI targets the right deployment.

## 6. Try Slack after the API works

Mention the bot in a test channel where the Slack app is installed:

```text
@<your bot's username> reply with exactly PONG
```

Slack messages without a harness flag use Codex. Add a selector such as
`--amp`, `--claude`, or `--pi` only when you want to override the default.

If Slack receives the mention but no agent runs, inspect Slackbot logs:

```bash
just logs slackbot
```
