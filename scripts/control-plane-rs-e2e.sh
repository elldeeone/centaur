#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$ROOT/services/control-plane-rs"
CONTAINER="centaur-control-plane-e2e-$RANDOM-$RANDOM"
API_KEY="aiv2_control_plane_e2e"
DB_PASSWORD="centaur_e2e"
SERVER_PID=""
FIRST_STEER_RESPONSE=""

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$FIRST_STEER_RESPONSE" ]]; then
    rm -f "$FIRST_STEER_RESPONSE"
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d \
  --name "$CONTAINER" \
  -e POSTGRES_USER=centaur \
  -e POSTGRES_PASSWORD="$DB_PASSWORD" \
  -e POSTGRES_DB=centaur \
  -p 127.0.0.1::5432 \
  postgres:16-alpine >/dev/null

DB_PORT="$(docker port "$CONTAINER" 5432/tcp | sed 's/.*://')"
DATABASE_URL="postgres://centaur:${DB_PASSWORD}@127.0.0.1:${DB_PORT}/centaur"

until docker exec "$CONTAINER" psql -U centaur -d centaur -c 'SELECT 1' >/dev/null 2>&1; do
  sleep 0.2
done

docker exec -i "$CONTAINER" psql -U centaur -d centaur >/dev/null <<SQL
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    scopes TEXT[] NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);
INSERT INTO api_keys (name, key_prefix, key_hash, scopes, created_by)
VALUES (
  'control-plane-e2e',
  substring('${API_KEY}' from 1 for 8),
  encode(digest('${API_KEY}', 'sha256'), 'hex'),
  ARRAY['agent'],
  'e2e'
);
SQL

pushd "$SERVICE_DIR" >/dev/null
CONTROL_PLANE_BIND=127.0.0.1:18080 \
CONTROL_PLANE_SANDBOX_BACKEND=fake \
CONTROL_PLANE_FAKE_TURN_DELAY_MS=1500 \
DATABASE_URL="$DATABASE_URL" \
cargo run >"$ROOT/.control-plane-rs-e2e.log" 2>&1 &
SERVER_PID="$!"
popd >/dev/null

for _ in {1..100}; do
  if curl -fsS http://127.0.0.1:18080/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

RESPONSE="$(
  curl -fsS -N \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -X POST http://127.0.0.1:18080/agent-turns \
    -d '{
      "request_id": "e2e-req-1",
      "thread_ref": "e2e-thread-1",
      "actor_ref": {"source": "e2e"},
      "runtime": {"harness": "codex"},
      "system_prompt": {"persona_id": "eng"},
      "content": [{"type": "text", "text": "reply with pong"}],
      "turn_policy": "steer"
    }'
)"

printf '%s\n' "$RESPONSE"
grep -q 'event: execution.started' <<<"$RESPONSE"
grep -q 'event: sandbox.ready' <<<"$RESPONSE"
grep -q 'event: harness.raw' <<<"$RESPONSE"
grep -q 'event: execution.terminal' <<<"$RESPONSE"
grep -q 'fake sandbox completed turn' <<<"$RESPONSE"

DEDUP_RESPONSE="$(
  curl -fsS -N \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -X POST http://127.0.0.1:18080/agent-turns \
    -d '{
      "request_id": "e2e-req-1",
      "thread_ref": "e2e-thread-1",
      "actor_ref": {"source": "e2e"},
      "runtime": {"harness": "codex"},
      "content": [{"type": "text", "text": "duplicate"}]
    }'
)"

printf '%s\n' "$DEDUP_RESPONSE"
grep -q 'event: execution.duplicate' <<<"$DEDUP_RESPONSE"

FIRST_STEER_RESPONSE="$(mktemp)"
curl -fsS -N \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:18080/agent-turns \
  -d '{
    "request_id": "e2e-req-active",
    "thread_ref": "e2e-thread-steer",
    "actor_ref": {"source": "e2e"},
    "runtime": {"harness": "codex"},
    "system_prompt": {"persona_id": "eng"},
    "content": [{"type": "text", "text": "stay busy briefly"}]
  }' >"$FIRST_STEER_RESPONSE" &
FIRST_STEER_PID="$!"

for _ in {1..100}; do
  if docker exec "$CONTAINER" psql -U centaur -d centaur -tAc \
    "SELECT COUNT(*) FROM control_agent_executions WHERE request_id = 'e2e-req-active'" \
    | grep -q '^1$'; then
    break
  fi
  sleep 0.02
done

STEER_RESPONSE="$(
  curl -fsS -N \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -X POST http://127.0.0.1:18080/agent-turns \
    -d '{
      "request_id": "e2e-req-steer",
      "thread_ref": "e2e-thread-steer",
      "actor_ref": {"source": "e2e"},
      "runtime": {"harness": "codex"},
      "content": [{"type": "text", "text": "steer active turn"}]
    }'
)"

wait "$FIRST_STEER_PID"
printf '%s\n' "$STEER_RESPONSE"
grep -q 'event: harness.raw' <<<"$STEER_RESPONSE"
grep -q 'fake_sandbox_steered' <<<"$STEER_RESPONSE"
grep -q '"status":"steered"' <<<"$STEER_RESPONSE"
grep -q 'fake sandbox completed turn' "$FIRST_STEER_RESPONSE"

echo "control-plane-rs e2e passed"
