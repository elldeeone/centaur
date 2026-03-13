#!/usr/bin/env bash
set -euo pipefail

source /app/bootstrap-secrets.sh

bootstrap_required_secrets SLACK_BOT_TOKEN SLACK_SIGNING_SECRET API_SECRET_KEY DATABASE_URL

exec "$@"
