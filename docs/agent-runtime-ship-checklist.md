# Agent Runtime One-Shot Ship Checklist

## Current State

1. Durable control plane exists for `spawn -> message -> execute`, durable execution events, and final-delivery outbox.
2. SlackBot uses durable event streaming + outbox ack paths.
3. Spawn gating and assignment-generation stale-write protection are enforced.
4. Runtime terminal harness errors are now classified as `failed_permanent` (not `completed`) and surfaced durably.
5. Runtime credential guardrail is implemented (`RUNTIME_CREDENTIAL_GUARD_ENABLED`) and blocks startup/readiness when required keys are missing.

## Remaining Gaps To Close

1. Legacy `/agent/connect` and `/agent/reconnect` are removed from the runtime path; only explicit `410 LEGACY_ENDPOINT_REMOVED` stubs remain for deterministic client failure.
2. Historical reconnect-timeout executions are corrected by migration `009`; production verification should confirm the migration has run everywhere.
3. Amp execution quality in local compose still depends on real `AMP_API_KEY` availability through firewall/secrets; without it, runs deterministically fail with reconnect timeout.
4. Durable worker currently reuses legacy inject/flush plumbing; this is valid but not yet fully isolated from legacy runtime module boundaries.
5. Long-horizon retention/compaction policy for `agent_execution_events` is not yet specified as an operational SLO.

## One-Shot Task List

1. Keep legacy wire endpoints as explicit `410` stubs only, and keep public docs and typed clients on the durable control-plane APIs.
2. Add migration/backfill to reclassify historical reconnect-timeout terminals from `completed` to `failed_permanent` where applicable.
3. Enable `RUNTIME_CREDENTIAL_GUARD_ENABLED=1` in non-dev deployment environments and set `REQUIRED_RUNTIME_SECRET_KEYS` explicitly.
4. Move remaining execution-path dependencies from legacy `api.agent` helpers into dedicated runtime-control abstractions.
5. Add retention and pruning policy for `agent_execution_events` with explicit replay window and `STREAM_RANGE_GONE` behavior.

## Test Matrix Required For Ship

1. API unit/integration: `uv run pytest -q` in `services/api`.
2. SlackBot tests: `pnpm --filter centaur-slackbot test`.
3. Repeatable external runtime QA: `scripts/qa-agent-runtime-flow.sh`.
4. Repeatable full tool QA: `python3 scripts/qa-full-tools.py`.
5. Repeatable combined runtime + tools QA: `scripts/qa-agent-runtime-and-tools.sh`.
6. Compose e2e happy-path: `spawn -> message -> execute -> events -> final-delivery ack`.
7. Compose e2e negative-path: no-spawn rejection, stale-generation rejection, cancel-then-follow-up recovery, terminal harness error classification (`failed_permanent`).
8. Compose e2e attachment-path: inline base64 content rewritten to `attachment_ref` and blob persisted in `attachments`.

## Ship Gate

1. No execution accepted without active spawn assignment generation.
2. Every execution creates final-delivery obligation row before runtime I/O.
3. Slack final response delivery remains recoverable after API restart and stream disconnect.
4. Warm runtime assignment remains pinned to one Slack thread and runtime-id drift is synchronized on replacement.
