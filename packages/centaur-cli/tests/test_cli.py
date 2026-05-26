from __future__ import annotations

import json

from typer.testing import CliRunner

from centaur_cli.main import app
from centaur_cli.templates import slack_manifest

runner = CliRunner()


def test_slack_manifest_uses_public_urls() -> None:
    manifest = slack_manifest("centaur", "centaur.example.com", socket_mode=False)

    assert manifest["settings"]["event_subscriptions"]["request_url"] == "https://centaur.example.com/slack/events"
    assert manifest["settings"]["interactivity"]["request_url"] == "https://centaur.example.com/slack/interactivity"
    assert "chat:write" in manifest["oauth_config"]["scopes"]["bot"]


def test_slack_manifest_socket_mode_removes_request_urls() -> None:
    manifest = slack_manifest("centaur", "centaur.example.com", socket_mode=True)

    assert manifest["settings"]["socket_mode_enabled"] is True
    assert "request_url" not in manifest["settings"]["event_subscriptions"]
    assert "request_url" not in manifest["settings"]["interactivity"]


def test_init_non_interactive_scaffolds_overlay(tmp_path) -> None:
    overlay = tmp_path / "org"
    home = tmp_path / "home"
    result = runner.invoke(
        app,
        [
            "init",
            "--non-interactive",
            "--org",
            "acme",
            "--assistant-name",
            "centaur",
            "--domain",
            "centaur.acme.com",
            "--overlay-path",
            str(overlay),
            "--home",
            str(home),
        ],
    )

    assert result.exit_code == 0
    assert (overlay / "AGENTS.md").exists()
    assert (overlay / "slack-app-manifest.json").exists()
    state = json.loads((home / "onboarding-state.json").read_text())
    assert state["org"] == "acme"
    assert "slack-manifest" in state["completed_steps"]


def test_deploy_kind_prints_local_cluster_commands() -> None:
    result = runner.invoke(app, ["deploy", "kind", "--cluster-name", "dogfood"])

    assert result.exit_code == 0
    assert "kind create cluster --name dogfood" in result.stdout
    assert "helm upgrade --install centaur contrib/chart" in result.stdout
