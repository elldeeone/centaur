#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
COMPOSE=(docker compose -f "$REPO_ROOT/docker-compose.yml")
REQUIRED_SERVICES=(api slackbot)

for service in "${REQUIRED_SERVICES[@]}"; do
  if ! "${COMPOSE[@]}" ps --services --status running | grep -qx "$service"; then
    echo "$service service is not running. Start the compose stack first." >&2
    echo "Suggested command: docker compose up -d secrets firewall postgres pgbouncer docker-socket-proxy api slackbot" >&2
    exit 1
  fi
done

python3 "$SCRIPT_DIR/qa_agent_runtime_flow.py" "$@"
