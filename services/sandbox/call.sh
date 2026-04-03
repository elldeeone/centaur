#!/bin/bash
# call — token-efficient API tool caller (returns TOON)
# Usage:
#   call <tool> <method> [json_body]   → POST /tools/<tool>/<method>
#   call tools                          → GET /tools (list all)
#   call discover <tool>               → GET /tools/<tool>
U="${CENTAUR_API_URL:-http://api:8000}"
T="Accept: text/plain"
J="Content-Type: application/json"
# Prefer refreshed token (written on warm-pool claim) over original env var
_KEY="${CENTAUR_API_KEY:-}"
if [ -f /home/agent/.api_key ]; then
  _KEY="$(cat /home/agent/.api_key)"
fi
A="Authorization: Bearer ${_KEY}"
tool="$1"
method="$2"
body="$3"

auth_headers=()
if [ -n "${_KEY}" ]; then
  auth_headers=(-H "$A")
fi

request() {
  local http_method="$1"
  local url="$2"
  local data="${3:-}"
  local timeout_s="${CALL_TIMEOUT_SECONDS:-30}"

  local curl_args=(
    -sS
    --max-time "$timeout_s"
    --retry 2
    --retry-connrefused
    -X "$http_method"
    "${auth_headers[@]}"
    -H "$T"
    "$url"
  )
  if [ -n "${CENTAUR_THREAD_KEY:-}" ]; then
    curl_args+=(-H "X-Trace-Id: ${CENTAUR_THREAD_KEY}")
  fi
  if [ -n "$data" ]; then
    curl_args+=(-H "$J" -d "$data")
  fi

  local response
  response="$(curl "${curl_args[@]}" --write-out $'\n__HTTP_STATUS__:%{http_code}')"
  local curl_exit=$?
  if [ "$curl_exit" -ne 0 ]; then
    printf '{"error":"transport_error","exit_code":%d,"url":%s}\n' \
      "$curl_exit" \
      "$(printf '%s' "$url" | jq -Rs .)"
    return 1
  fi

  local status="${response##*__HTTP_STATUS__:}"
  local body="${response%$'\n'__HTTP_STATUS__:*}"
  if [[ "$status" =~ ^2 ]]; then
    printf '%s\n' "$body"
    return 0
  fi

  local snippet="${body:0:1200}"
  printf '{"error":"http_error","status":%s,"url":%s,"body":%s}\n' \
    "$status" \
    "$(printf '%s' "$url" | jq -Rs .)" \
    "$(printf '%s' "$snippet" | jq -Rs .)"
  return 1
}

agent_execute() {
  local payload="$1"

  if [ -z "$payload" ]; then
    printf '%s\n' '{"error":"invalid_request","message":"call agent execute requires a JSON body"}'
    return 1
  fi

  if ! printf '%s' "$payload" | jq -e . >/dev/null 2>&1; then
    printf '{"error":"invalid_json","body":%s}\n' "$(printf '%s' "$payload" | jq -Rs .)"
    return 1
  fi

  if printf '%s' "$payload" | jq -e 'has("assignment_generation")' >/dev/null 2>&1; then
    request "POST" "$U/agent/execute" "$payload"
    return $?
  fi

  if ! printf '%s' "$payload" | jq -e '(.message | type) == "string" and (.message | length) > 0' >/dev/null 2>&1; then
    request "POST" "$U/agent/execute" "$payload"
    return $?
  fi

  local nonce
  nonce="call-agent-$(date +%s)-$RANDOM"

  local spawn_payload
  spawn_payload="$(printf '%s' "$payload" | jq -c --arg spawn_id "${nonce}:spawn" '
    {
      thread_key,
      spawn_id: (.spawn_id // $spawn_id)
    }
    + (if .harness != null then {harness: .harness} else {} end)
    + (if .engine != null then {engine: .engine} else {} end)
    + (if .persona_id != null then {persona_id: .persona_id} else {} end)
    + (if .agents_md_override != null then {agents_md_override: .agents_md_override} else {} end)
  ')"

  local spawn_response
  spawn_response="$(request "POST" "$U/agent/spawn" "$spawn_payload")" || {
    printf '%s\n' "$spawn_response"
    return 1
  }

  local assignment_generation
  assignment_generation="$(printf '%s' "$spawn_response" | jq -r '.assignment_generation // empty')"
  if ! [[ "$assignment_generation" =~ ^[0-9]+$ ]] || [ "$assignment_generation" -le 0 ]; then
    printf '{"error":"invalid_spawn_response","body":%s}\n' "$(printf '%s' "$spawn_response" | jq -Rs .)"
    return 1
  fi

  local message_payload
  message_payload="$(printf '%s' "$payload" | jq -c --argjson assignment_generation "$assignment_generation" --arg message_id "${nonce}:message" '
    {
      thread_key,
      assignment_generation: $assignment_generation,
      message_id: (.message_id // $message_id),
      role: (.role // "user"),
      parts: [{type: "text", text: .message}]
    }
    + (if .user_id != null then {user_id: .user_id} else {} end)
    + (if .metadata != null then {metadata: .metadata} else {} end)
  ')"

  local message_response
  message_response="$(request "POST" "$U/agent/message" "$message_payload")" || {
    printf '%s\n' "$message_response"
    return 1
  }

  local execute_payload
  execute_payload="$(printf '%s' "$payload" | jq -c --argjson assignment_generation "$assignment_generation" --arg execute_id "${nonce}:execute" '
    {
      thread_key,
      assignment_generation: $assignment_generation,
      execute_id: (.execute_id // $execute_id)
    }
    + (if .harness != null then {harness: .harness} else {} end)
    + (if .delivery != null then {delivery: .delivery} else {} end)
    + (if .platform != null then {platform: .platform} else {} end)
    + (if .user_id != null then {user_id: .user_id} else {} end)
    + (if .metadata != null then {metadata: .metadata} else {} end)
  ')"

  request "POST" "$U/agent/execute" "$execute_payload"
}

case "$tool" in
  search)
    printf '%s\n' '{"error":"deprecated_command","command":"call search","replacement":"Use direct tool calls such as `call websearch search '\''{\"query\":\"...\"}'\''` or `call slack search_messages '\''{\"query\":\"...\"}'\''`."}'
    exit 1
    ;;
  sql)
    printf '%s\n' '{"error":"deprecated_command","command":"call sql","replacement":"Use a tool-specific query method such as `call paradigmdb db_query '\''{\"query\":\"SELECT ...\"}'\''` or `call paradigmdb bq_query ...`."}'
    exit 1
    ;;
  tools)
    request "GET" "$U/tools"
    ;;
  discover)
    request "GET" "$U/tools/$2"
    ;;
  agent)
    # Usage: call agent execute '{"thread_key":"...","message":"...","harness":"legal"}'
    #        call agent execute '{"thread_key":"...","assignment_generation":1,...}'
    #        call agent stop '{"thread_key":"..."}'
    #        call agent status '?key=...'
    if [ "$method" = "status" ]; then
      request "GET" "$U/agent/status$body"
    elif [ "$method" = "execute" ]; then
      agent_execute "$body"
    else
      request "POST" "$U/agent/$method" "$body"
    fi
    ;;
  *)
    if [ -z "$body" ]; then
      request "POST" "$U/tools/$tool/$method"
    else
      request "POST" "$U/tools/$tool/$method" "$body"
    fi
    ;;
esac
