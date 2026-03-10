#!/bin/bash
# slack-upload — upload a file to the current Slack thread
# Usage: slack-upload <file_path> [comment]
set -euo pipefail

if [ -z "${1:-}" ]; then
  echo "Usage: slack-upload <file_path> [comment]" >&2
  exit 1
fi

FILE="$1"
COMMENT="${2:-}"

if [ ! -f "$FILE" ]; then
  echo "Error: file not found: $FILE" >&2
  exit 1
fi

U="${AI_V2_API_URL:-http://api:8000}"
CHANNEL="${SLACK_CHANNEL:?SLACK_CHANNEL not set}"
THREAD="${SLACK_THREAD_TS:?SLACK_THREAD_TS not set}"
FILENAME="$(basename "$FILE")"
B64="$(base64 -w0 "$FILE")"

BODY=$(jq -nc \
  --arg content_base64 "$B64" \
  --arg filename "$FILENAME" \
  --arg comment "$COMMENT" \
  --arg channel "$CHANNEL" \
  --arg thread_ts "$THREAD" \
  '{content_base64: $content_base64, filename: $filename, comment: $comment, channel: $channel, thread_ts: $thread_ts}')

_KEY="${AI_V2_API_KEY:-}"
if [ -f /home/agent/.api_key ]; then
  _KEY="$(cat /home/agent/.api_key)"
fi
AUTH_ARGS=()
if [ -n "${_KEY}" ]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${_KEY}")
fi

RESP=$(curl -sf "${AUTH_ARGS[@]}" -H "Content-Type: application/json" -d "$BODY" "$U/tools/slack/upload_file") || {
  echo "Error: upload failed" >&2
  exit 1
}

LINK=$(echo "$RESP" | jq -r '.permalink // .file.permalink // empty' 2>/dev/null)
if [ -n "$LINK" ]; then
  echo "$LINK"
else
  echo "$RESP"
fi
