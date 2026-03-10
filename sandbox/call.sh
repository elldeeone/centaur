#!/bin/bash
# call — token-efficient API tool caller (returns TOON)
# Usage:
#   call <tool> <method> [json_body]   → POST /tools/<tool>/<method>
#   call search <query> [limit]        → POST /api/search
#   call sql <query>                   → POST /api/search/sql
#   call discover <tool>               → GET /tools/<tool>
U="${AI_V2_API_URL:-http://api:8000}"
T="Accept: text/plain"
J="Content-Type: application/json"
# Prefer refreshed token (written on warm-pool claim) over original env var
_KEY="${AI_V2_API_KEY:-}"
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

case "$tool" in
  search)
    request "POST" "$U/api/search" "{\"query\":$(printf '%s' "$2" | jq -Rs .),\"limit\":${3:-20}}"
    ;;
  sql)
    request "POST" "$U/api/search/sql" "{\"query\":$(printf '%s' "$2" | jq -Rs .)}"
    ;;
  discover)
    request "GET" "$U/tools/$2"
    ;;
  *)
    if [ -z "$body" ]; then
      request "POST" "$U/tools/$tool/$method"
    else
      request "POST" "$U/tools/$tool/$method" "$body"
    fi
    ;;
esac
