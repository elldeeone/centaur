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

case "$1" in
  search)
    curl -s -H "$J" -H "$T" -d "{\"query\":$(printf '%s' "$2" | jq -Rs .),\"limit\":${3:-20}}" "$U/api/search"
    ;;
  sql)
    curl -s -H "$J" -H "$T" -d "{\"query\":$(printf '%s' "$2" | jq -Rs .)}" "$U/api/search/sql"
    ;;
  discover)
    curl -s "$U/tools/$2"
    ;;
  *)
    if [ -z "$3" ]; then
      curl -s -H "$T" -X POST "$U/tools/$1/$2"
    else
      curl -s -H "$J" -H "$T" -d "$3" "$U/tools/$1/$2"
    fi
    ;;
esac
