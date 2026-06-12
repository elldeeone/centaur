from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ENTRYPOINT_SH = Path(__file__).resolve().parents[2] / "sandbox" / "entrypoint.sh"


def _write_codex_harness_config(home: Path) -> Path:
    harness_dir = home / "harness"
    codex_dir = harness_dir / "codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "\n".join(
            [
                'model = "gpt-5.5"',
                'model_reasoning_effort = "low"',
                'plan_mode_reasoning_effort = "high"',
                'approval_policy = "on-request"',
                'approvals_reviewer = "user"',
                'web_search = "live"',
                'personality = "pragmatic"',
                'sandbox_mode = "workspace-write"',
                "check_for_update_on_startup = true",
                "suppress_unstable_features_warning = true",
                'service_tier = "fast"',
                "",
                "[tools]",
                "view_image = true",
                "",
                "[features]",
                "goals = true",
                "memories = true",
                "code_mode = true",
                "hooks = true",
                "browser_use = true",
                "computer_use = true",
                "enable_fanout = true",
                "runtime_metrics = true",
                "",
                "[features.multi_agent_v2]",
                "enabled = true",
                "max_concurrent_threads_per_session = 6",
                "",
                "[agents]",
                "max_depth = 2",
                "job_max_runtime_seconds = 1800",
                "",
            ]
        )
    )
    return harness_dir


def test_sandbox_entrypoint_bootstraps_mock_google_adc(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "amp").mkdir(parents=True)
    harness_dir = _write_codex_harness_config(home)

    result = subprocess.run(
        [
            "bash",
            str(ENTRYPOINT_SH),
            "sh",
            "-lc",
            'printf \'%s\n\' "$GOOGLE_APPLICATION_CREDENTIALS" && cat "$GOOGLE_APPLICATION_CREDENTIALS"',
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CENTAUR_HARNESS_CONFIG_DIR": str(harness_dir),
        },
    )

    assert result.returncode == 0, result.stderr or result.stdout
    adc_path, adc_json = result.stdout.split("\n", 1)
    assert adc_path == str(
        home / ".config" / "gcloud" / "application_default_credentials.json"
    )
    assert Path(adc_path).is_file()
    adc = json.loads(adc_json)
    assert adc == {
        "type": "service_account",
        "project_id": "centaur-sandbox",
        "private_key_id": "0000000000000000000000000000000000000000",
        "private_key": adc["private_key"],
        "client_email": "mock@creds.com",
        "client_id": "100000000000000000000",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/mock%40creds.com",
        "universe_domain": "googleapis.com",
    }
    assert adc["private_key"].startswith("-----BEGIN PRIVATE KEY-----\n")
    assert adc["private_key"].endswith("-----END PRIVATE KEY-----\n")

    codex_config = (home / ".codex" / "config.toml").read_text()
    assert 'model = "gpt-5.5"' in codex_config
    assert 'model_reasoning_effort = "low"' in codex_config
    assert 'plan_mode_reasoning_effort = "high"' in codex_config
    assert 'approval_policy = "on-request"' in codex_config
    assert 'sandbox_mode = "workspace-write"' in codex_config
    assert 'service_tier = "fast"' in codex_config
    assert "max_concurrent_threads_per_session = 6" in codex_config


def test_sandbox_entrypoint_installs_codex_harness_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    harness_dir = _write_codex_harness_config(home)

    result = subprocess.run(
        [
            "bash",
            str(ENTRYPOINT_SH),
            "sh",
            "-lc",
            'cat "$HOME/.codex/config.toml"',
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CENTAUR_HARNESS_CONFIG_DIR": str(harness_dir),
        },
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert 'model = "gpt-5.5"' in result.stdout
    assert 'multi_agent = false' in result.stdout
    assert 'multi_agent_v2 = false' in result.stdout


def test_sandbox_entrypoint_exports_pythonpath_for_tool_clis(tmp_path: Path) -> None:
    home = tmp_path / "home"
    harness_dir = _write_codex_harness_config(home)
    tools_dir = home / "github" / "centaur" / "tools"
    tools_dir.mkdir(parents=True)

    result = subprocess.run(
        [
            "bash",
            str(ENTRYPOINT_SH),
            "sh",
            "-lc",
            'printf "%s" "$PYTHONPATH"',
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CENTAUR_HARNESS_CONFIG_DIR": str(harness_dir),
            "TOOL_DIRS": str(tools_dir),
        },
    )

    assert result.returncode == 0, result.stderr or result.stdout
    entries = result.stdout.split(":")
    assert str(home / "github" / "centaur") in entries


def test_sandbox_entrypoint_installs_overlay_skills_and_prompt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    harness_dir = _write_codex_harness_config(home)
    overlay_dir = tmp_path / "overlay" / "org"
    (overlay_dir / ".agents" / "skills" / "intendo-ops").mkdir(parents=True)
    (overlay_dir / ".agents" / "skills" / "intendo-ops" / "SKILL.md").write_text(
        "# Intendo Ops\n"
    )
    (overlay_dir / "services" / "sandbox").mkdir(parents=True)
    (overlay_dir / "services" / "sandbox" / "SYSTEM_PROMPT.md").write_text(
        "# Intendo Org Context\n\nPrefer org tools for Intendo shorthand.\n"
    )
    (home / "AGENTS.md").write_text("# Base Prompt\n")

    result = subprocess.run(
        [
            "bash",
            str(ENTRYPOINT_SH),
            "sh",
            "-lc",
            "printf '%s\\n---skills---\\n' \"$(cat AGENTS.md)\" && find .agents/skills -type f | sort",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CENTAUR_HARNESS_CONFIG_DIR": str(harness_dir),
            "CENTAUR_OVERLAY_DIR": str(overlay_dir),
        },
    )

    assert result.returncode == 0, result.stderr or result.stdout
    prompt, skills = result.stdout.split("\n---skills---\n", 1)
    assert "# Base Prompt" in prompt
    assert "# Intendo Org Context" in prompt
    assert "Prefer org tools for Intendo shorthand." in prompt
    assert ".agents/skills/intendo-ops/SKILL.md" in skills
