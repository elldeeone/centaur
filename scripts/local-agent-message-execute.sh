#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
fi

API_URL="${API_URL:-http://localhost:8000}"
API_KEY="${API_KEY:-${LOCAL_DEV_API_KEY:-${SLACKBOT_API_KEY:-}}}"
PROMPT="${1:-Reply with exactly PONG and nothing else.}"
THREAD_KEY="local-smoke:$(date +%s)-${RANDOM}"
TMPDIR="$(mktemp -d)"
EXECUTION_ID=""

if [[ -z "$API_KEY" ]]; then
  echo "No API key found." >&2
  echo "Set API_KEY directly, or define LOCAL_DEV_API_KEY / SLACKBOT_API_KEY in .env." >&2
  exit 1
fi

json_field() {
  local file="$1"
  local key="$2"
  python3 - "$file" "$key" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], 'r', encoding='utf-8'))
value = data
for part in sys.argv[2].split('.'):
    if part:
        value = value[part]
if isinstance(value, (dict, list)):
    print(json.dumps(value))
else:
    print(value)
PY
}

cleanup() {
  set +e
  if [[ -n "$EXECUTION_ID" ]]; then
    curl -sS -X POST "$API_URL/agent/final-deliveries/$EXECUTION_ID/delivered" \
      -H "Authorization: Bearer $API_KEY" \
      -H 'Content-Type: application/json' \
      --data '{}' >/dev/null
  fi
  local encoded_thread_key
  encoded_thread_key="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$THREAD_KEY")"
  curl -sS -X POST "$API_URL/agent/threads/$encoded_thread_key/release" \
    -H "Authorization: Bearer $API_KEY" \
    -H 'Content-Type: application/json' \
    --data '{"cancel_inflight":true}' >/dev/null
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

echo "Checking local stack health..."
curl -fsS "$API_URL/health" >/dev/null

echo "Spawning assignment for $THREAD_KEY"
curl -sS -X POST "$API_URL/agent/spawn" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data "$(python3 - <<PY
import json
print(json.dumps({
  'thread_key': '$THREAD_KEY',
  'harness': 'amp',
}))
PY
)" > "$TMPDIR/spawn.json"
ASSIGNMENT_GENERATION="$(json_field "$TMPDIR/spawn.json" assignment_generation)"

echo "Posting message"
curl -sS -X POST "$API_URL/agent/message" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data "$(THREAD_KEY_INPUT="$THREAD_KEY" ASSIGNMENT_GENERATION_INPUT="$ASSIGNMENT_GENERATION" PROMPT_INPUT="$PROMPT" python3 - <<'PY'
import json, os
print(json.dumps({
  'thread_key': os.environ['THREAD_KEY_INPUT'],
  'assignment_generation': int(os.environ['ASSIGNMENT_GENERATION_INPUT']),
  'role': 'user',
  'parts': [{'type': 'text', 'text': os.environ['PROMPT_INPUT']}],
}))
PY
)" > "$TMPDIR/message.json"

echo "Executing"
curl -sS -X POST "$API_URL/agent/execute" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data "$(python3 - <<PY
import json
print(json.dumps({
  'thread_key': '$THREAD_KEY',
  'assignment_generation': int('$ASSIGNMENT_GENERATION'),
  'harness': 'amp',
  'delivery': {'platform': 'qa'},
}))
PY
)" > "$TMPDIR/execute.json"
EXECUTION_ID="$(json_field "$TMPDIR/execute.json" execution_id)"

echo "Polling $EXECUTION_ID"
while true; do
  curl -sS "$API_URL/agent/executions/$EXECUTION_ID" \
    -H "Authorization: Bearer $API_KEY" \
    > "$TMPDIR/status.json"
  STATUS="$(json_field "$TMPDIR/status.json" status)"
  RESULT_TEXT="$(python3 - "$TMPDIR/status.json" <<'PY'
import json, sys
obj = json.load(open(sys.argv[1], 'r', encoding='utf-8'))
print(obj.get('result_text', ''))
PY
)"
  echo "  status=$STATUS"
  if [[ "$STATUS" == "completed" ]]; then
    echo
    echo "result_text=$RESULT_TEXT"
    break
  fi
  if [[ "$STATUS" != "queued" && "$STATUS" != "claimed" && "$STATUS" != "running" ]]; then
    echo
    cat "$TMPDIR/status.json"
    exit 1
  fi
  sleep 1
done

echo
echo "Done."
