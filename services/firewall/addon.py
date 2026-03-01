"""Firewall addon — host-based credential injection.

Intercepts ALL outgoing HTTPS requests from sandbox containers. For known
API hosts, unconditionally injects the appropriate credential header with
real secrets fetched on demand from the secret manager service.

No placeholder detection — credentials are set based solely on the target host.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from mitmproxy import http

log = logging.getLogger("firewall")

SECRET_MANAGER_URL = os.environ.get("SECRET_MANAGER_URL", "http://secrets:8100")
CACHE_TTL = int(os.environ.get("FIREWALL_CACHE_TTL", "30"))
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8081"))

BLOCKED_HOSTS: frozenset[str] = frozenset({
    "secrets",
    "169.254.169.254",
})

# Host → (secret_key, header_name, style)
# Styles: "raw"    → value is the secret itself
#         "bearer" → "Bearer {secret}"
#         "token"  → "token {secret}"
#         "basic"  → "Basic base64(x-access-token:{secret})"
_HOST_RULES: dict[str, tuple[str, str, str]] = {
    "api.anthropic.com": ("ANTHROPIC_API_KEY", "x-api-key", "raw"),
    "api.openai.com": ("OPENAI_API_KEY", "authorization", "bearer"),
    "ampcode.com": ("AMP_API_KEY", "authorization", "bearer"),
    "api.ampcode.com": ("AMP_API_KEY", "authorization", "bearer"),
    "api.github.com": ("GITHUB_TOKEN", "authorization", "token"),
    "github.com": ("GITHUB_TOKEN", "authorization", "basic"),
    "uploads.github.com": ("GITHUB_TOKEN", "authorization", "token"),
}


def _format_header(secret: str, style: str) -> str:
    if style == "bearer":
        return f"Bearer {secret}"
    if style == "token":
        return f"token {secret}"
    if style == "basic":
        raw = f"x-access-token:{secret}"
        return f"Basic {base64.b64encode(raw.encode()).decode()}"
    return secret


class CredentialInjector:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._lock = threading.Lock()
        log.info("credential injector started (host rules: %s)", ", ".join(sorted(_HOST_RULES)))
        self._start_health_server()

    def _start_health_server(self) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/health":
                    with parent._lock:
                        cached = sum(1 for v, _ in parent._cache.values() if v is not None)
                    body = json.dumps({"status": "ok", "secrets_cached": cached})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body.encode())
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, fmt: str, *args: object) -> None:
                pass

        def serve() -> None:
            server = HTTPServer(("0.0.0.0", HEALTH_PORT), Handler)
            server.serve_forever()

        threading.Thread(target=serve, daemon=True).start()

    def _get_secret(self, key: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and (now - cached[1]) < CACHE_TTL:
                return cached[0]

        try:
            url = f"{SECRET_MANAGER_URL}/secrets/{urllib.parse.quote(key, safe='')}"
            with urllib.request.urlopen(url, timeout=3) as resp:
                val = json.loads(resp.read().decode()).get("value")
        except Exception:
            val = None

        with self._lock:
            self._cache[key] = (val, now)

        if val is None:
            log.warning("secret %s: not found in secret manager", key)
        return val

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host.lower().rstrip(".")

        if host in BLOCKED_HOSTS:
            flow.response = http.Response.make(
                403, b"Blocked by security policy", {"content-type": "text/plain"},
            )
            log.warning("blocked request to %s", host)
            return

        rule = _HOST_RULES.get(host)
        if rule is None:
            return

        secret_key, header_name, style = rule
        secret = self._get_secret(secret_key)
        if secret is None:
            log.warning("no secret for %s (key=%s) — passing request unmodified", host, secret_key)
            return

        flow.request.headers[header_name] = _format_header(secret, style)


addons = [CredentialInjector()]
