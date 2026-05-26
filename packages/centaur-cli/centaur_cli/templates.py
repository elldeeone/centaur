from __future__ import annotations

import json
from pathlib import Path

SLACK_SCOPES = [
    "app_mentions:read",
    "channels:history",
    "channels:read",
    "chat:write",
    "files:read",
    "files:write",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "users:read",
]


def slack_manifest(app_name: str, domain: str, socket_mode: bool) -> dict:
    base_url = f"https://{domain}".rstrip("/")
    manifest = {
        "display_information": {"name": app_name},
        "features": {
            "bot_user": {"display_name": app_name, "always_online": True},
            "slash_commands": [
                {
                    "command": "/centaur",
                    "description": "Send a command to Centaur",
                    "url": f"{base_url}/slack/commands",
                    "should_escape": False,
                }
            ],
        },
        "oauth_config": {"scopes": {"bot": SLACK_SCOPES}},
        "settings": {
            "interactivity": {
                "is_enabled": True,
                "request_url": f"{base_url}/slack/interactivity",
            },
            "event_subscriptions": {
                "request_url": f"{base_url}/slack/events",
                "bot_events": ["app_mention", "message.im", "file_shared"],
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": socket_mode,
            "token_rotation_enabled": False,
        },
    }
    if socket_mode:
        manifest["settings"]["event_subscriptions"].pop("request_url", None)
        manifest["settings"]["interactivity"].pop("request_url", None)
    return manifest


def write_overlay(path: Path, org: str, assistant_name: str, domain: str) -> list[Path]:
    path.mkdir(parents=True, exist_ok=True)
    files = {
        "AGENTS.md": f"""# {assistant_name}

You are {assistant_name}, the AI assistant for {org}.

## Operating Rules

- Be direct and concrete.
- Verify external writes before claiming success.
- Ask before taking destructive actions.
- Use the configured Centaur tools before ad hoc external calls.

## Deployment

- Domain: {domain or "unset"}
- Overlay owner: {org}
""",
        "secrets.example.env": """# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_APP_TOKEN=xapp-...
SLACK_CLIENT_ID=...
SLACK_CLIENT_SECRET=...

# Models
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
OPENROUTER_API_KEY=...

# GitHub
GITHUB_APP_ID=...
GITHUB_APP_PRIVATE_KEY=...
GITHUB_INSTALLATION_ID=...

# Centaur
SLACKBOT_API_KEY=...
DATABASE_URL=postgres://...
""",
        "values.centaur.yaml": f"""global:
  domain: {domain or "centaur.example.com"}
  overlay:
    enabled: true
    mountPath: /overlay/org

slackbot:
  enabled: true

api:
  ingress:
    enabled: true
""",
        "personas/base.md": f"""You are {assistant_name}. Follow the overlay instructions in AGENTS.md.""",
        "skills/README.md": "# Skills\n\nAdd organization-specific Centaur skills here.\n",
        "tools/README.md": "# Tools\n\nAdd organization-specific tool wrappers here.\n",
        "workflows/README.md": "# Workflows\n\nAdd durable workflow definitions here.\n",
    }
    written: list[Path] = []
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(content)
            written.append(target)
    return written


def write_slack_manifest(path: Path, app_name: str, domain: str, socket_mode: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(slack_manifest(app_name, domain, socket_mode), indent=2) + "\n")
    return path
