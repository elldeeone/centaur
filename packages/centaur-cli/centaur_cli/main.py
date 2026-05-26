from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .checks import binary_checks, command_check, docker_daemon_check, env_checks, overlay_checks
from .state import DEFAULT_HOME, OnboardingState, load_state, save_state
from .templates import SLACK_SCOPES, write_overlay, write_slack_manifest

app = typer.Typer(name="centaur", help="Centaur onboarding and operations CLI")
overlay_app = typer.Typer(help="Create and validate Centaur overlays")
integrations_app = typer.Typer(help="Generate and verify integration setup")
deploy_app = typer.Typer(help="Prepare Centaur deployments")
secrets_app = typer.Typer(help="Validate secret backend setup")
app.add_typer(overlay_app, name="overlay")
app.add_typer(integrations_app, name="integrations")
app.add_typer(deploy_app, name="deploy")
app.add_typer(secrets_app, name="secrets")

console = Console()


def _ask(prompt: str, default: str, non_interactive: bool) -> str:
    if non_interactive:
        return default
    return typer.prompt(prompt, default=default)


def _render_results(results) -> bool:
    table = Table("Check", "Status", "Detail", "Repair")
    all_ok = True
    for result in results:
        all_ok = all_ok and result.ok
        table.add_row(result.name, "ok" if result.ok else "fail", result.detail, result.repair)
    console.print(table)
    return all_ok


@app.command()
def init(
    org: str = typer.Option("", help="Organization name"),
    assistant_name: str = typer.Option("centaur", help="Assistant display name"),
    domain: str = typer.Option("", help="Public deployment domain"),
    admin_email: str = typer.Option("", help="Admin email"),
    install_mode: str = typer.Option("local", help="local, k8s, or ssh"),
    secret_backend: str = typer.Option("local-env", help="local-env, onepassword, doppler, vault, sops, or kubernetes"),
    overlay_path: Path = typer.Option(Path("org"), help="Overlay directory to create or validate"),
    home: Path = typer.Option(DEFAULT_HOME, help="Centaur config directory"),
    resume: bool = typer.Option(False, help="Resume from existing onboarding state"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Use provided/default values without prompts"),
) -> None:
    """Run the guided Centaur onboarding wizard."""
    state = load_state(home) if resume else OnboardingState()
    console.print(Panel("Centaur will set up an overlay, secrets plan, Slack, model, GitHub, and deployment checklist."))

    state.org = _ask("Organization name", org or state.org or "acme", non_interactive)
    state.assistant_name = _ask("Assistant name", assistant_name or state.assistant_name, non_interactive)
    state.domain = _ask("Public domain", domain or state.domain or "centaur.example.com", non_interactive)
    state.admin_email = _ask("Admin email", admin_email or state.admin_email or "admin@example.com", non_interactive)
    state.install_mode = _ask("Install mode (local/k8s/ssh)", install_mode or state.install_mode, non_interactive)
    state.secret_backend = _ask("Secret backend", secret_backend or state.secret_backend, non_interactive)
    state.overlay_path = str(overlay_path)

    written = write_overlay(overlay_path, state.org, state.assistant_name, state.domain)
    write_slack_manifest(overlay_path / "slack-app-manifest.json", state.assistant_name, state.domain, socket_mode=state.install_mode == "local")
    for step in ["local-state", "overlay", "slack-manifest", "secrets-plan", "deployment-plan"]:
        state.mark_done(step)
    save_state(state, home)

    console.print(f"Wrote onboarding state to {home / 'onboarding-state.json'}")
    console.print(f"Overlay path: {overlay_path}")
    if written:
        console.print("Created:")
        for path in written:
            console.print(f"  - {path}")
    console.print("\nNext checks:")
    _render_results(binary_checks(include_deploy=state.install_mode in {"k8s", "ssh"}, include_ssh=state.install_mode == "ssh") + overlay_checks(overlay_path))
    console.print("\nRun `centaur integrations slack-manifest --domain %s` to print the Slack app manifest." % state.domain)


@app.command()
def doctor(
    deep: bool = typer.Option(False, help="Include deploy and environment checks"),
    overlay_path: Path = typer.Option(Path("org"), help="Overlay path"),
) -> None:
    """Check local prerequisites and generated Centaur setup files."""
    results = binary_checks(include_deploy=deep) + overlay_checks(overlay_path)
    if deep:
        results += env_checks()
        results.append(docker_daemon_check())
        if any(result.name == "binary:kubectl" and result.ok for result in results):
            results.append(command_check("kubectl:cluster", ["kubectl", "cluster-info"], "Select a working Kubernetes context or use centaur deploy kind."))
        if any(result.name == "binary:helm" and result.ok for result in results):
            results.append(command_check("helm:version", ["helm", "version", "--short"], "Install Helm before deploying to Kubernetes."))
    ok = _render_results(results)
    raise typer.Exit(0 if ok else 1)


@app.command()
def status(home: Path = typer.Option(DEFAULT_HOME, help="Centaur config directory")) -> None:
    """Show resumable onboarding state."""
    state = load_state(home)
    console.print_json(json.dumps(state.__dict__, default=str))


@app.command("smoke-test")
def smoke_test(namespace: str = "centaur", release: str = "centaur") -> None:
    """Print the exact commands for an end-to-end Centaur smoke test."""
    console.print("Run this after deployment:")
    console.print(f"just namespace={namespace} release={release} smoke")


@overlay_app.command("init")
def overlay_init(
    path: Path = typer.Option(Path("org"), help="Overlay directory"),
    org: str = typer.Option("acme", help="Organization name"),
    assistant_name: str = typer.Option("centaur", help="Assistant name"),
    domain: str = typer.Option("centaur.example.com", help="Deployment domain"),
) -> None:
    """Scaffold a Centaur overlay repo."""
    written = write_overlay(path, org, assistant_name, domain)
    write_slack_manifest(path / "slack-app-manifest.json", assistant_name, domain, socket_mode=False)
    console.print(f"Overlay ready at {path}")
    for item in written:
        console.print(f"  - {item}")


@overlay_app.command("validate")
def overlay_validate(path: Path = typer.Option(Path("org"), help="Overlay directory")) -> None:
    """Validate required overlay files."""
    ok = _render_results(overlay_checks(path))
    raise typer.Exit(0 if ok else 1)


@integrations_app.command("slack-manifest")
def slack_manifest_cmd(
    domain: str = typer.Option("centaur.example.com", help="Public Centaur domain"),
    app_name: str = typer.Option("centaur", help="Slack app name"),
    socket_mode: bool = typer.Option(False, help="Use Socket Mode instead of public request URLs"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write manifest to a file"),
) -> None:
    """Generate the Slack app manifest with scopes, events, commands, and interactivity."""
    path = output or Path("/tmp/centaur-slack-app-manifest.json")
    write_slack_manifest(path, app_name, domain, socket_mode)
    text = path.read_text()
    if output:
        console.print(f"Wrote Slack manifest to {output}")
    else:
        console.print(text)
    console.print("Required bot scopes: " + ", ".join(SLACK_SCOPES))
    console.print("After installing the app, store SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, and optionally SLACK_APP_TOKEN.")


@integrations_app.command("setup")
def integrations_setup() -> None:
    """Print the baseline manual setup checklist for Slack, models, and GitHub."""
    console.print(
        """Baseline setup:
1. Slack: create app from `centaur integrations slack-manifest`, install it, copy bot token/signing secret/app token.
2. Models: add OPENAI_API_KEY, ANTHROPIC_API_KEY, or OPENROUTER_API_KEY to your secret backend.
3. GitHub: create a GitHub App with contents/pull-requests/issues/actions permissions, then store app id/private key/installation id.
4. Verify with `centaur doctor --deep` after secrets are exported or synced into Kubernetes."""
    )


@secrets_app.command("doctor")
def secrets_doctor(backend: str = typer.Option("local-env", help="local-env, onepassword, sops, doppler, vault, or kubernetes")) -> None:
    """Validate the selected secret backend enough to continue onboarding."""
    results = []
    if backend == "onepassword":
        results.append(command_check("1password:op", ["op", "vault", "list"], "Set OP_SERVICE_ACCOUNT_TOKEN and run `op vault list`."))
    elif backend == "sops":
        results.append(command_check("sops:version", ["sops", "--version"], "Install sops."))
        results.append(command_check("age:version", ["age", "--version"], "Install age and generate a key."))
    elif backend == "kubernetes":
        results.append(command_check("kubectl:secrets", ["kubectl", "get", "secret", "-A"], "Create Kubernetes secrets or configure cluster access."))
    else:
        results += env_checks()
    ok = _render_results(results)
    raise typer.Exit(0 if ok else 1)


@deploy_app.command("k8s")
def deploy_k8s(namespace: str = "centaur", release: str = "centaur", values: Path = Path("org/values.centaur.yaml")) -> None:
    """Print the existing-cluster deployment command."""
    console.print("Existing Kubernetes deployment:")
    console.print(f"kubectl create namespace {namespace} --dry-run=client -o yaml | kubectl apply -f -")
    console.print(f"helm upgrade --install {release} contrib/chart -n {namespace} -f {values}")


@deploy_app.command("kind")
def deploy_kind(
    cluster_name: str = typer.Option("centaur", help="kind cluster name"),
    namespace: str = typer.Option("centaur", help="Kubernetes namespace"),
    release: str = typer.Option("centaur", help="Helm release name"),
    values: Path = typer.Option(Path("org/values.centaur.yaml"), help="Helm values file"),
    apply: bool = typer.Option(False, "--apply", help="Run the commands instead of printing them"),
) -> None:
    """Create a local kind cluster and deploy Centaur into it."""
    commands = [
        ["kind", "create", "cluster", "--name", cluster_name],
        ["kubectl", "cluster-info", "--context", f"kind-{cluster_name}"],
        ["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "yaml"],
        ["helm", "dependency", "update", "contrib/chart"],
        ["helm", "upgrade", "--install", release, "contrib/chart", "-n", namespace, "-f", str(values)],
    ]
    if not apply:
        console.print("Local kind deployment:")
        console.print(f"kind create cluster --name {cluster_name}")
        console.print(f"kubectl cluster-info --context kind-{cluster_name}")
        console.print(f"kubectl create namespace {namespace} --dry-run=client -o yaml | kubectl apply -f -")
        console.print("helm dependency update contrib/chart")
        console.print(f"helm upgrade --install {release} contrib/chart -n {namespace} -f {values}")
        console.print("Add `--apply` to run these commands.")
        return

    import subprocess

    for command in commands[:2]:
        subprocess.run(command, check=True)
    namespace_yaml = subprocess.run(commands[2], check=True, capture_output=True)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=namespace_yaml.stdout, check=True)
    for command in commands[3:]:
        subprocess.run(command, check=True)


@deploy_app.command("ssh")
def deploy_ssh(host: str, domain: str = typer.Option(..., help="Public domain for this host")) -> None:
    """Print the SSH/k3s bootstrap plan for a new server."""
    console.print(f"SSH deployment plan for {host}:")
    console.print("1. ssh into host and install k3s")
    console.print("2. copy kubeconfig locally")
    console.print("3. install ingress-nginx, cert-manager, and ArgoCD")
    console.print(f"4. point DNS for {domain} at the host")
    console.print("5. run `centaur deploy k8s` once the kube context works")


@app.command()
def logs(component: str = "api", namespace: str = "centaur", release: str = "centaur") -> None:
    """Print the kubectl log command for a Centaur component."""
    console.print(f"kubectl logs -n {namespace} deploy/{release}-centaur-{component} --tail=200 -f")


@app.command()
def repair(step: str) -> None:
    """Print focused repair instructions for one onboarding area."""
    repairs = {
        "slack": "Regenerate the manifest, update Slack request URLs, reinstall the app, then rerun auth.test and a test mention.",
        "github": "Check GitHub App permissions, installation id, private key formatting, and webhook delivery status.",
        "secrets": "Run `centaur secrets doctor --backend <backend>` and sync missing keys into the selected backend.",
        "deploy": "Run `centaur doctor --deep`, fix cluster/helm failures, then rerun `centaur deploy k8s`.",
    }
    console.print(repairs.get(step, "Known repair steps: slack, github, secrets, deploy"))


if __name__ == "__main__":
    app()
