#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "tool-qa-output"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
STATUS_MARKER = "__CENTAUR_QA_HTTP_STATUS__:"
DEFAULT_INTERNAL_URL = "http://localhost:8000"
DEFAULT_EXTERNAL_URL = "http://localhost:8000"
DEFAULT_LOCAL_API_KEY_ENV_KEYS = (
    "QA_FULL_TOOLS_API_KEY",
    "LOCAL_DEV_API_KEY",
)


def load_repo_dotenv() -> dict[str, str]:
    path = REPO_ROOT / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values.setdefault(key, value)
    return values


def resolve_local_api_key() -> tuple[str, str] | None:
    dotenv = load_repo_dotenv()
    for env_var in DEFAULT_LOCAL_API_KEY_ENV_KEYS:
        token = os.environ.get(env_var, "").strip() or dotenv.get(env_var, "").strip()
        if token:
            return env_var, token
    return None


PROBE_OVERRIDES: dict[str, dict[str, Any]] = {
    "addepar": {"method": "list_entities", "payload": {"limit": 2}},
    "affinity": {"method": "whoami", "payload": {}},
    "alchemy": {"method": "get_block_number", "payload": {}},
    "allium": {"method": "search_schemas", "payload": {"query": "bitcoin"}},
    "alphasense": {"method": "search", "payload": {"limit": 2, "query": "bitcoin"}},
    "anchorage": {"method": "list_staking_delegations", "payload": {"limit": 2}},
    "arkham": {"method": "health", "payload": {}},
    "ashby": {"method": "users", "payload": {"limit": 2}},
    "attio": {"method": "list_lists", "payload": {}},
    "bitgo": {"method": "list_enterprises", "payload": {}},
    "bloomberg": {"method": "get_catalog", "payload": {}},
    "coingecko": {"method": "get_categories", "payload": {}},
    "coinmetrics": {"method": "list_assets", "payload": {}},
    "confmonitor": {"method": "get_sheet_data", "payload": {}},
    "congress": {"method": "list_bills", "payload": {"limit": 2}},
    "crunchbase": {
        "method": "search_acquisitions",
        "payload": {"field_ids": ["bitcoin"], "limit": 2},
    },
    "databento": {
        "method": "get_stock_prices",
        "payload": {"end_date": "2026-03-03", "start_date": "2026-03-01", "symbol": "BTC"},
    },
    "debank": {"method": "get_chain_list", "payload": {}},
    "defillama": {"method": "list_chains", "payload": {}},
    "demo": {"method": "ping", "payload": {}},
    "docusign": {"method": "users", "payload": {"limit": 2}},
    "dune": {"method": "get_query", "payload": {"query_id": 1}},
    "eodhd": {"method": "get_eod_prices", "payload": {"symbol": "BTC"}},
    "etherscan": {"method": "get_gas_price", "payload": {}},
    "events": {"method": "get_upcoming_events", "payload": {}},
    "falconx": {"method": "list_pairs", "payload": {}},
    "fedreg": {"method": "get_agencies", "payload": {}},
    "granola": {"method": "list_all_notes", "payload": {"limit": 2}},
    "gsuite": {"method": "calendar_list", "payload": {}},
    "investmemos": {"method": "list_memos", "payload": {"limit": 2}},
    "ironclad": {"method": "entities", "payload": {"limit": 2}},
    "jpm": {"method": "get_cash_balances", "payload": {"start_date": "2026-03-01"}},
    "kalshi": {"method": "list_events", "payload": {"limit": 2}},
    "karma": {"method": "list_daos", "payload": {}},
    "legal-playbook": {"method": "get_policy_version", "payload": {}},
    "legistorm": {"method": "get_caucuses_retired_ids", "payload": {}},
    "linear": {"method": "me", "payload": {}},
    "listennotes": {"method": "search", "payload": {"query": "bitcoin"}},
    "messari": {"method": "list_assets", "payload": {"limit": 2}},
    "nansen": {"method": "search_entity", "payload": {"per_page": 2, "query": "bitcoin"}},
    "newsapi": {"method": "search", "payload": {"page_size": 2, "q": "bitcoin"}},
    "notion": {"method": "me", "payload": {}},
    "openfec": {"method": "search_candidates", "payload": {"per_page": 2}},
    "paradigmdb": {"method": "db_tables", "payload": {}},
    "plural": {"method": "list_jurisdictions", "payload": {"per_page": 2}},
    "polymarket": {"method": "list_events", "payload": {"limit": 2}},
    "posthog": {"method": "events", "payload": {"limit": 2}},
    "pylon": {"method": "list_accounts", "payload": {"limit": 2}},
    "reth": {"method": "get_execution_timings", "payload": {}},
    "sensortower": {"method": "search_apps", "payload": {"limit": 2, "query": "bitcoin"}},
    "sigma": {"method": "list_members", "payload": {"limit": 2}},
    "similarweb": {"method": "get_categories", "payload": {}},
    "slack": {"method": "list_channels", "payload": {"limit": 2}},
    "snapshot": {"method": "list_spaces", "payload": {}},
    "social-monitor": {"method": "stats", "payload": {}},
    "standard-metrics": {"method": "list_companies", "payload": {"page_size": 2}},
    "tally": {"method": "list_governors", "payload": {"limit": 2}},
    "termsheet": {"method": "list_deals", "payload": {}},
    "token-terminal": {"method": "list_market_sectors", "payload": {}},
    "tokenomist": {"method": "list_tokens", "payload": {"limit": 2}},
    "transcriber": {"method": "list_models", "payload": {}},
    "twitter": {"method": "get_usage", "payload": {}},
    "unit410": {"method": "list_wallets", "payload": {}},
    "vlogs": {"method": "ready", "payload": {}},
    "websearch": {"method": "search", "payload": {"num_results": 2, "query": "bitcoin"}},
    "youtube": {"method": "search", "payload": {"max_results": 2, "query": "bitcoin"}},
}

SKIP_TOOLS = {"coinbase", "figma", "grafana", "harmonic", "profslice"}

DEFAULT_PARAM_VALUES: dict[str, Any] = {
    "limit": 2,
    "max_results": 2,
    "page_size": 2,
    "per_page": 2,
    "page": 1,
    "offset": 0,
    "query": "bitcoin",
    "q": "bitcoin",
    "search": "bitcoin",
    "keyword": "bitcoin",
    "keywords": "bitcoin",
    "symbol": "BTC",
    "asset": "btc",
    "assets": "btc",
    "ids": "bitcoin",
    "vs_currency": "usd",
    "vs_currencies": "usd",
    "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    "wallet": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    "contract": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "domain": "paradigm.xyz",
    "username": "VitalikButerin",
    "handle": "VitalikButerin",
    "chain": "ethereum",
    "network": "ethereum",
    "table_name": "Fund",
    "start_date": "2026-03-01",
    "end_date": "2026-03-03",
    "from_date": "2026-03-01",
    "to_date": "2026-03-03",
}

SAFE_METHOD_PREFIXES = (
    "health",
    "ready",
    "whoami",
    "me",
    "stats",
    "list_",
    "get_",
    "search",
    "lookup",
    "query",
    "read",
    "fetch",
    "find",
    "describe",
)

MUTATING_METHOD_PREFIXES = (
    "create",
    "update",
    "delete",
    "send",
    "upload",
    "append",
    "archive",
    "restore",
    "void",
    "approve",
    "submit",
    "mark",
    "execute",
    "cancel",
    "run_",
    "reload",
    "close",
    "connect",
    "disconnect",
    "save",
    "import",
    "add_",
    "remove_",
    "mint",
    "set_",
)


@dataclass
class HttpResponse:
    http_status: int | None
    body: Any
    body_text: str


def run_command(
    args: list[str],
    *,
    input_text: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=REPO_ROOT,
        check=False,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a repeatable two-layer read-only QA sweep across all registered tools.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for JSON and Markdown artifacts (default: %(default)s)",
    )
    parser.add_argument(
        "--prefix",
        default="full-tool-qa",
        help="Artifact filename prefix (default: %(default)s)",
    )
    parser.add_argument(
        "--tool-timeout",
        type=float,
        default=float(os.environ.get("QA_FULL_TOOLS_TIMEOUT_S", "45")),
        help="Per-request timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--api-container",
        default=os.environ.get("QA_FULL_TOOLS_API_CONTAINER", "centaur-api-1"),
        help="Container name for direct internal API calls (default: %(default)s)",
    )
    parser.add_argument(
        "--api-service",
        default=os.environ.get("QA_FULL_TOOLS_API_SERVICE", "api"),
        help="Compose service name used for stack preflight (default: %(default)s)",
    )
    parser.add_argument(
        "--internal-url",
        default=os.environ.get("QA_FULL_TOOLS_INTERNAL_URL", DEFAULT_INTERNAL_URL),
        help="Base URL for internal API calls executed inside the API container (default: %(default)s)",
    )
    parser.add_argument(
        "--external-url",
        default=os.environ.get("QA_FULL_TOOLS_EXTERNAL_URL", DEFAULT_EXTERNAL_URL),
        help="Base URL for authenticated API replay (default: %(default)s)",
    )
    return parser.parse_args()


def require_running_service(api_service: str) -> None:
    proc = run_command(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "ps",
            "--services",
            "--status",
            "running",
        ]
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or proc.stdout.strip() or "docker compose ps failed")
    services = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    if api_service not in services:
        raise SystemExit(
            textwrap.dedent(
                f"""
                {api_service} service is not running.
                Start the compose stack first, for example:
                docker compose up -d secrets firewall postgres pgbouncer docker-socket-proxy api slackbot
                """
            ).strip()
        )


def sanitize_text(text: str) -> str:
    value = text
    value = re.sub(r"(API Key:\s*)([^\\\n]+)", r"\1[REDACTED]", value, flags=re.IGNORECASE)
    value = re.sub(r"(Client ID:\s*)([^\\\n]+)", r"\1[REDACTED]", value, flags=re.IGNORECASE)
    value = re.sub(r"(Client Secret:\s*)([^\\\n]+)", r"\1[REDACTED]", value, flags=re.IGNORECASE)
    value = re.sub(
        r"(Invalid username in Basic auth \()(\'[^\']+\')(\))",
        r"\1\'[REDACTED]\'\3",
        value,
        flags=re.IGNORECASE,
    )
    for pattern, replacement in [
        (
            r'("?(?:api[_ -]?key|token|secret|client[_ -]?secret|client[_ -]?id)"?\s*[:=]\s*["\'])([^"\'\n]+)(["\'])',
            r"\1[REDACTED]\3",
        ),
        (
            r"((?:api[_ -]?key|token|secret|client[_ -]?secret|client[_ -]?id)\s*[:=]\s*)([^,\s\n]+)",
            r"\1[REDACTED]",
        ),
    ]:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return value


def curl_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: Any | None = None,
    via_container: str | None = None,
    timeout_s: float,
) -> HttpResponse:
    data = json.dumps(payload) if payload is not None else None
    cmd: list[str] = []
    if via_container:
        cmd.extend(["docker", "exec"])
        if data is not None:
            cmd.append("-i")
        cmd.append(via_container)
    cmd.extend(
        [
            "curl",
            "-sS",
            "--max-time",
            str(timeout_s),
            "-X",
            method,
            "-o",
            "-",
            "-w",
            f"\\n{STATUS_MARKER}%{{http_code}}",
            url,
        ]
    )
    if headers:
        for name, value in headers.items():
            cmd.extend(["-H", f"{name}: {value}"])
    if data is not None:
        cmd.extend(["-H", "Content-Type: application/json", "--data-binary", "@-"])
    proc = run_command(cmd, input_text=data, timeout=timeout_s + 5)
    if proc.returncode != 0:
        body_text = sanitize_text((proc.stderr or proc.stdout or "curl failed").strip())
        return HttpResponse(http_status=None, body=None, body_text=body_text)
    stdout = proc.stdout
    if STATUS_MARKER not in stdout:
        body_text = sanitize_text(stdout.strip())
        return HttpResponse(http_status=None, body=None, body_text=body_text)
    raw_body, _, raw_status = stdout.rpartition(f"\n{STATUS_MARKER}")
    try:
        status = int(raw_status.strip())
    except ValueError:
        status = None
    body_text = raw_body.strip()
    parsed: Any
    if body_text:
        try:
            parsed = json.loads(body_text)
        except json.JSONDecodeError:
            parsed = body_text
    else:
        parsed = None
    return HttpResponse(http_status=status, body=parsed, body_text=sanitize_text(body_text))


def get_services_snapshot() -> list[dict[str, str]]:
    proc = run_command(["docker", "compose", "-f", str(COMPOSE_FILE), "ps", "-a", "--format", "json"])
    if proc.returncode != 0:
        return [{"name": "docker-compose", "status": sanitize_text(proc.stderr.strip() or proc.stdout.strip())}]
    services: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        services.append({"name": item.get("Name", "unknown"), "status": item.get("Status", "unknown")})
    services.sort(key=lambda item: item["name"])
    return services


def fetch_tools_index(args: argparse.Namespace) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {args.local_api_key}"} if getattr(args, "local_api_key", None) else None
    response = curl_request(
        f"{args.external_url if headers else args.internal_url}/tools",
        headers=headers,
        via_container=None if headers else args.api_container,
        timeout_s=args.tool_timeout,
    )
    if response.http_status != 200 or not isinstance(response.body, dict):
        raise RuntimeError(f"GET /tools failed: status={response.http_status} body={response.body_text}")
    return response.body


def fetch_tool_detail(args: argparse.Namespace, tool: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {args.local_api_key}"} if getattr(args, "local_api_key", None) else None
    response = curl_request(
        f"{args.external_url if headers else args.internal_url}/tools/{tool}",
        headers=headers,
        via_container=None if headers else args.api_container,
        timeout_s=args.tool_timeout,
    )
    if response.http_status != 200 or not isinstance(response.body, dict):
        raise RuntimeError(f"GET /tools/{tool} failed: status={response.http_status} body={response.body_text}")
    return response.body


def is_safe_method(name: str) -> bool:
    lower = name.lower()
    if lower.startswith(MUTATING_METHOD_PREFIXES):
        return False
    return lower.startswith(SAFE_METHOD_PREFIXES)


def build_payload_from_schema(params: dict[str, Any]) -> dict[str, Any] | None:
    payload: dict[str, Any] = {}
    for param_name, meta in params.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("required"):
            if param_name not in DEFAULT_PARAM_VALUES:
                return None
            payload[param_name] = DEFAULT_PARAM_VALUES[param_name]
            continue
        if param_name in {"limit", "page_size", "per_page", "max_results"}:
            payload[param_name] = 2
    return payload


def choose_probe(tool: str, detail: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if tool in SKIP_TOOLS:
        return None
    override = PROBE_OVERRIDES.get(tool)
    if override:
        return override["method"], dict(override.get("payload", {}))
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for method in detail.get("methods", []):
        if not isinstance(method, dict):
            continue
        name = method.get("name")
        if not isinstance(name, str) or not is_safe_method(name):
            continue
        payload = build_payload_from_schema(method.get("parameters", {}))
        if payload is None:
            continue
        score = 0
        lower = name.lower()
        if lower in {"health", "ready", "ping", "whoami", "me", "stats"}:
            score += 50
        if lower.startswith("list_"):
            score += 40
        elif lower.startswith("get_"):
            score += 30
        elif lower.startswith("search"):
            score += 20
        score -= len(payload)
        candidates.append((score, name, payload))
    if not candidates:
        return None
    _, name, payload = sorted(candidates, key=lambda item: (-item[0], item[1]))[0]
    return name, payload


def preview_success(body: Any) -> str:
    if isinstance(body, dict):
        keys = ", ".join(sorted(str(key) for key in body.keys())[:6])
        return f"dict keys: {keys}" if keys else "dict"
    if isinstance(body, list):
        return f"list[{len(body)}]"
    if isinstance(body, str):
        collapsed = " ".join(body.split())
        return sanitize_text(collapsed[:160] or "string")
    if body is None:
        return "empty body"
    return sanitize_text(str(body)[:160])


def looks_empty(body: Any, body_text: str) -> bool:
    if body in (None, [], {}, ""):
        return True
    return body_text.strip() in {"", "[]", "{}", "null"}


def unwrap_tool_envelope(body: Any, body_text: str) -> tuple[Any, str]:
    current = body
    if isinstance(current, dict):
        if isinstance(current.get("error"), str):
            current = current["error"]
        elif isinstance(current.get("detail"), str):
            current = current["detail"]
        elif "result" in current:
            current = current["result"]
    if isinstance(current, str):
        stripped = current.strip()
        if stripped and stripped[0] in "[{\"":
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = current
            else:
                current = parsed
    if isinstance(current, (dict, list)):
        effective_text = sanitize_text(json.dumps(current, separators=(",", ":")))
    elif current is None:
        effective_text = ""
    else:
        effective_text = sanitize_text(str(current))
    if not effective_text:
        effective_text = sanitize_text(body_text)
    return current, effective_text


def classify_response(response: HttpResponse) -> tuple[str, str]:
    if response.http_status is None:
        return "fail_runtime", response.body_text or "request failed before an HTTP status was returned"
    effective_body, effective_text = unwrap_tool_envelope(response.body, response.body_text)
    text = effective_text.strip()
    lower = text.lower()
    if response.http_status >= 500:
        return "fail_runtime", text or f"HTTP {response.http_status}"
    if any(
        token in lower
        for token in [
            "missing api key",
            "invalid api key",
            "authentication failed",
            "invalid or expired token",
            "unauthorized",
            "401 unauthorized",
            "api key not valid",
            "access token",
            "forbidden",
            "not allowlisted",
            "no api key found",
            "authentication_error",
            "api key provided was invalid",
            "invalid api token",
            "not authorized to access",
            "requires authentication",
            "authentication failed",
            "api key lacks required permissions",
        ]
    ):
        return "fail_auth", text or f"HTTP {response.http_status}"
    if response.http_status in {400, 404, 422} or any(
        token in lower
        for token in [
            "invalid field",
            "unexpected keyword argument",
            "expecting value",
            "does not exist",
            "relation \"",
            "validation error",
            "missing required",
            "bad request",
            "unknown parameter",
            "no such column",
        ]
    ):
        return "fail_schema", text or f"HTTP {response.http_status}"
    if any(
        token in lower
        for token in [
            "no more credits",
            "could not parse the provided public key",
            "non-hexadecimal number found",
            "no such file or directory",
            "timed out",
            "connection refused",
            "temporarily unavailable",
            "traceback",
            "internal server error",
            "api error: 500",
        ]
    ):
        return "fail_runtime", text or f"HTTP {response.http_status}"
    if looks_empty(effective_body, effective_text):
        return "warn_empty", text or "empty response"
    return "pass", preview_success(effective_body)


def invoke_tool(
    *,
    args: argparse.Namespace,
    layer: str,
    tool: str,
    method: str,
    payload: dict[str, Any],
    api_key: str | None = None,
) -> dict[str, Any]:
    if layer == "layer1":
        headers = {"Authorization": f"Bearer {args.local_api_key}"} if getattr(args, "local_api_key", None) else None
        response = curl_request(
            f"{args.external_url if headers else args.internal_url}/tools/{tool}/{method}",
            method="POST",
            headers=headers,
            payload=payload,
            via_container=None if headers else args.api_container,
            timeout_s=args.tool_timeout,
        )
    elif layer == "layer2":
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        response = curl_request(
            f"{args.external_url}/tools/{tool}/{method}",
            method="POST",
            headers=headers,
            payload=payload,
            timeout_s=args.tool_timeout,
        )
    else:
        raise ValueError(f"unsupported layer {layer}")
    classification, note = classify_response(response)
    return {
        "tool": tool,
        "method": method,
        "payload": payload,
        "http_status": response.http_status,
        "classification": classification,
        "note": sanitize_text(note[:280]),
    }


def summarize_tools(results: dict[str, dict[str, Any]]) -> dict[str, int]:
    counter = Counter(result["classification"] for result in results.values())
    return dict(sorted(counter.items()))


def mint_layer2_key(args: argparse.Namespace) -> tuple[str, str]:
    payload = {
        "name": f"qa-full-tools-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        "scopes": ["tools:*"],
        "created_by": "scripts/qa-full-tools.py",
    }
    headers = {"Authorization": f"Bearer {args.local_api_key}"} if getattr(args, "local_api_key", None) else None
    response = curl_request(
        f"{args.external_url if headers else args.internal_url}/admin/api-keys",
        method="POST",
        headers=headers,
        payload=payload,
        via_container=None if headers else args.api_container,
        timeout_s=args.tool_timeout,
    )
    if response.http_status != 200 or not isinstance(response.body, dict):
        raise RuntimeError(f"failed to mint layer2 API key: {response.body_text}")
    key = response.body.get("key")
    key_id = response.body.get("id")
    if not isinstance(key, str) or not isinstance(key_id, str):
        raise RuntimeError("admin/api-keys did not return both key and id")
    return key, key_id


def revoke_layer2_key(args: argparse.Namespace, key_id: str) -> None:
    headers = {"Authorization": f"Bearer {args.local_api_key}"} if getattr(args, "local_api_key", None) else None
    response = curl_request(
        f"{args.external_url if headers else args.internal_url}/admin/api-keys/{key_id}",
        method="DELETE",
        headers=headers,
        via_container=None if headers else args.api_container,
        timeout_s=args.tool_timeout,
    )
    if response.http_status not in {200, 404}:
        raise RuntimeError(f"failed to revoke API key {key_id}: {response.body_text}")


def to_markdown(report: dict[str, Any], json_path: Path, md_path: Path) -> str:
    def icon(status: str) -> str:
        return "✅" if "up" in status.lower() else "❌"

    layer1_label = "Host API" if report.get("layer1_transport") == "external_host_key" else "Internal API"

    lines: list[str] = []
    lines.append("# Full Tool QA Report")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| **Date** | {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} |")
    lines.append("| **Tool scope** | full |")
    lines.append("| **Layer scope** | layers 1-2 |")
    lines.append(f"| **Raw results** | `{json_path}` |")
    lines.append("")
    lines.append("## Layer Summary")
    lines.append("")
    lines.append("| Layer | Status | Notes |")
    lines.append("|-------|--------|-------|")
    lines.append(
        f"| 1. {layer1_label} | ✅ | {report['tool_count']} tools loaded; "
        f"{', '.join(f'{count} {name}' for name, count in report['layer1']['summary'].items())} |"
    )
    lines.append(
        f"| 2. Authenticated API edge | ✅ | {', '.join(f'{count} {name}' for name, count in report['layer2']['summary'].items())} |"
    )
    lines.append("| 3a. Slackbot | ⏭️ | Not part of this tool-focused pass |")
    lines.append("")
    lines.append("## Services")
    lines.append("")
    lines.append("| Service | Status |")
    lines.append("|---------|--------|")
    for service in report.get("services", []):
        lines.append(f"| {service['name']} | {icon(service['status'])} {service['status']} |")
    lines.append("")
    lines.append("## Tool Summary")
    lines.append("")
    for layer_name in ("layer1", "layer2"):
        lines.append(f"### {layer_name.capitalize()}")
        lines.append("")
        lines.append("| Classification | Count |")
        lines.append("|----------------|-------|")
        for name, count in report[layer_name]["summary"].items():
            lines.append(f"| {name} | {count} |")
        lines.append("")
    lines.append("## Findings")
    lines.append("")
    for heading, classification in [
        ("Auth Failures", "fail_auth"),
        ("Runtime Failures", "fail_runtime"),
        ("Schema/Data Failures", "fail_schema"),
        ("Warnings", "warn_empty"),
    ]:
        lines.append(f"### {heading}")
        lines.append("")
        lines.append("| Tool | Method | Notes |")
        lines.append("|------|--------|-------|")
        for tool, result in sorted(report["layer1"]["tools"].items()):
            if result["classification"] != classification:
                continue
            lines.append(
                f"| {tool} | {result.get('method', '')} | {result.get('note', '').replace('|', '/')} |"
            )
        lines.append("")
    lines.append("### Skipped")
    lines.append("")
    lines.append("| Tool | Notes |")
    lines.append("|------|-------|")
    for tool, result in sorted(report["layer1"]["tools"].items()):
        if result["classification"] != "skip_no_safe_read_method":
            continue
        lines.append(f"| {tool} | {result.get('note', '').replace('|', '/')} |")
    lines.append("")
    lines.append("## API Key Notes")
    lines.append("")
    if report.get("layer1_transport") == "external_host_key":
        lines.append(
            "1. Layer 1 runs entirely from the host using the configured local dev key (`LOCAL_DEV_API_KEY` or `QA_FULL_TOOLS_API_KEY`)."
        )
        lines.append(
            "2. Layer 2 replays the same read-only probes through the configured authenticated API edge using an ephemeral DB-backed `aiv2_*` key minted and revoked entirely from the host."
        )
    else:
        lines.append(
            "1. Layer 2 replays the same read-only probes through the configured authenticated API edge using an ephemeral DB-backed `aiv2_*` key minted via the localhost admin route."
        )
        lines.append("2. The script revokes that temporary key automatically after the replay completes.")
    if report.get("layer_mismatches"):
        lines.append(f"3. Layer mismatches detected: {', '.join(report['layer_mismatches'])}.")
    else:
        lines.append("3. Layer 1 and layer 2 classifications matched for every probed tool in this run.")
    lines.append("")
    lines.append("## Output Files")
    lines.append("")
    lines.append(f"1. `{json_path}`")
    lines.append(f"2. `{md_path}`")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    local_api_key = resolve_local_api_key()
    args.local_api_key_source = local_api_key[0] if local_api_key else None
    args.local_api_key = local_api_key[1] if local_api_key else None
    require_running_service(args.api_service)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"{args.prefix}-{timestamp}.json"
    md_path = output_dir / f"{args.prefix}-{timestamp}.md"

    tools_index = fetch_tools_index(args)
    tool_details: dict[str, dict[str, Any]] = {}
    probes: dict[str, tuple[str, dict[str, Any]] | None] = {}

    for tool_name in sorted(tools_index):
        detail = fetch_tool_detail(args, tool_name)
        tool_details[tool_name] = detail
        probes[tool_name] = choose_probe(tool_name, detail)

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "tool_count": len(tools_index),
        "services": get_services_snapshot(),
        "layer1_transport": "external_host_key" if args.local_api_key else "internal_container",
        "local_api_key_source": args.local_api_key_source,
        "layer1": {"summary": {}, "tools": {}},
        "layer2": {"summary": {}, "tools": {}},
        "layer_mismatches": [],
    }

    for tool_name in sorted(tools_index):
        probe = probes[tool_name]
        if probe is None:
            report["layer1"]["tools"][tool_name] = {
                "tool": tool_name,
                "classification": "skip_no_safe_read_method",
                "note": "no safe read-only method with buildable inputs found",
            }
            continue
        method, payload = probe
        report["layer1"]["tools"][tool_name] = invoke_tool(
            args=args,
            layer="layer1",
            tool=tool_name,
            method=method,
            payload=payload,
        )

    report["layer1"]["summary"] = summarize_tools(report["layer1"]["tools"])

    api_key = ""
    key_id = ""
    try:
        api_key, key_id = mint_layer2_key(args)
        for tool_name in sorted(tools_index):
            probe = probes[tool_name]
            if probe is None:
                report["layer2"]["tools"][tool_name] = {
                    "tool": tool_name,
                    "classification": "skip_no_safe_read_method",
                    "note": "no safe read-only method with buildable inputs found",
                }
                continue
            method, payload = probe
            report["layer2"]["tools"][tool_name] = invoke_tool(
                args=args,
                layer="layer2",
                tool=tool_name,
                method=method,
                payload=payload,
                api_key=api_key,
            )
    finally:
        if key_id:
            revoke_layer2_key(args, key_id)

    report["layer2"]["summary"] = summarize_tools(report["layer2"]["tools"])

    mismatches: list[str] = []
    for tool_name in sorted(tools_index):
        left = report["layer1"]["tools"][tool_name]["classification"]
        right = report["layer2"]["tools"][tool_name]["classification"]
        if left != right:
            mismatches.append(f"{tool_name}: {left} -> {right}")
    report["layer_mismatches"] = mismatches

    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    md_path.write_text(to_markdown(report, json_path, md_path))

    print(json.dumps({
        "json": str(json_path),
        "markdown": str(md_path),
        "tool_count": report["tool_count"],
        "layer1": report["layer1"]["summary"],
        "layer2": report["layer2"]["summary"],
        "mismatches": report["layer_mismatches"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
