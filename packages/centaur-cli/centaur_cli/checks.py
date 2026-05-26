from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    repair: str = ""


REQUIRED_BINARIES = ["git", "jq", "openssl"]
DEPLOY_BINARIES = ["kubectl", "helm"]
OPTIONAL_BINARIES = ["gh", "docker", "kind", "ssh", "op", "sops", "age", "argocd"]


def binary_checks(include_deploy: bool = False, include_ssh: bool = False) -> list[CheckResult]:
    names = REQUIRED_BINARIES + OPTIONAL_BINARIES
    if include_deploy:
        names += DEPLOY_BINARIES
    results: list[CheckResult] = []
    for name in names:
        path = shutil.which(name)
        required = name in REQUIRED_BINARIES or (include_deploy and name in DEPLOY_BINARIES) or (include_ssh and name == "ssh")
        detail = path or ("missing optional" if not required else "not installed")
        results.append(
            CheckResult(
                name=f"binary:{name}",
                ok=path is not None or not required,
                detail=detail,
                repair=f"Install {name} and rerun centaur doctor" if required and not path else "",
            )
        )
    return results


def docker_daemon_check() -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult("docker:daemon", False, "docker not installed", "Install Docker or use an existing Kubernetes cluster.")
    return command_check("docker:daemon", ["docker", "info", "--format", "{{.ServerVersion}}"], "Start Docker Desktop or the Docker daemon.")


def env_checks() -> list[CheckResult]:
    checks = {
        "SLACK_BOT_TOKEN": "Create a Slack app and store the bot token.",
        "SLACK_SIGNING_SECRET": "Copy the Slack signing secret into your secret backend.",
        "OPENAI_API_KEY or ANTHROPIC_API_KEY": "Configure at least one model provider key.",
        "GITHUB_APP_ID or GITHUB_TOKEN": "Configure a GitHub App or PAT.",
    }
    results: list[CheckResult] = []
    for name, repair in checks.items():
        if " or " in name:
            keys = name.split(" or ")
            ok = any(os.getenv(key) for key in keys)
        else:
            ok = bool(os.getenv(name))
        results.append(CheckResult(name=f"env:{name}", ok=ok, detail="set" if ok else "missing", repair=repair if not ok else ""))
    return results


def command_check(name: str, cmd: list[str], repair: str) -> CheckResult:
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(name=name, ok=False, detail=str(exc), repair=repair)
    detail = (proc.stdout or proc.stderr).strip().splitlines()
    return CheckResult(name=name, ok=proc.returncode == 0, detail=detail[0] if detail else f"exit {proc.returncode}", repair="" if proc.returncode == 0 else repair)


def overlay_checks(path: Path) -> list[CheckResult]:
    required = ["AGENTS.md", "secrets.example.env", "values.centaur.yaml"]
    return [
        CheckResult(
            name=f"overlay:{rel}",
            ok=(path / rel).exists(),
            detail=str(path / rel),
            repair="" if (path / rel).exists() else f"Run centaur overlay init --path {path}",
        )
        for rel in required
    ]
