"""Firewall addon — stateless header-value credential replacement.

Intercepts ALL outgoing HTTPS requests from sandbox containers. Scans
every header value for known secret key names (fetched from the secret
manager) and replaces them with real secrets on the fly.

Container env vars contain the key name as the value (e.g.
``OPENAI_API_KEY=OPENAI_API_KEY``), so when a CLI sends
``Authorization: Bearer OPENAI_API_KEY`` the firewall replaces it with
``Authorization: Bearer sk-proj-real...``.

Amp routes LLM calls through ampcode.com/api/provider/{provider}/... which
requires a paid plan. To bypass this, the firewall rewrites these requests
to go directly to the real API endpoint (e.g. api.anthropic.com) with
key-name placeholders that the replacement logic resolves.
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
KEYS_REFRESH_INTERVAL = int(os.environ.get("KEYS_REFRESH_INTERVAL", "60"))

BLOCKED_HOSTS: frozenset[str] = frozenset(
    {
        "secrets",
        "postgres",
        "redis",
        "grafana",
        "prometheus",
        "loki",
        "promtail",
        "auth",
        "etl",
        "169.254.169.254",
    }
)

# Amp provider proxy rewriting: ampcode.com/api/provider/{provider}/...
# is rewritten to call the real API directly with key-name placeholders.
# prefix_to_strip → (real_host, header_name, header_value_template)
# Templates use the key name directly so the replacement logic resolves them.
_PROVIDER_REWRITES: dict[str, tuple[str, str, str]] = {
    "/api/provider/anthropic/": ("api.anthropic.com", "x-api-key", "ANTHROPIC_API_KEY"),
    "/api/provider/openai/": ("api.openai.com", "authorization", "Bearer OPENAI_API_KEY"),
}


class CredentialInjector:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._lock = threading.Lock()
        self._known_keys: set[str] = set()
        self._keys_lock = threading.Lock()
        log.info("credential injector started (stateless header-value replacement)")
        self._start_health_server()
        self._start_keys_refresh()

    # ------------------------------------------------------------------
    # Health server
    # ------------------------------------------------------------------

    def _start_health_server(self) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/health":
                    with parent._lock:
                        cached = sum(1 for v, _ in parent._cache.values() if v is not None)
                    with parent._keys_lock:
                        known = len(parent._known_keys)
                    body = json.dumps(
                        {
                            "status": "ok",
                            "secrets_cached": cached,
                            "known_keys": known,
                        }
                    )
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

    # ------------------------------------------------------------------
    # Known keys refresh (background thread)
    # ------------------------------------------------------------------

    def _start_keys_refresh(self) -> None:
        def loop() -> None:
            while True:
                self._refresh_keys()
                time.sleep(KEYS_REFRESH_INTERVAL)

        threading.Thread(target=loop, daemon=True).start()

    def _refresh_keys(self) -> None:
        try:
            url = f"{SECRET_MANAGER_URL}/keys"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            keys = set(data.get("keys", []))
            with self._keys_lock:
                self._known_keys = keys
            log.info("refreshed known keys: %d keys", len(keys))
        except Exception:
            log.warning("failed to refresh known keys from secret manager")

    # ------------------------------------------------------------------
    # Secret fetching (cached)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Header-value replacement
    # ------------------------------------------------------------------

    def _replace_key_names(self, value: str) -> str:
        """Replace any known key names in a header value with real secrets."""
        with self._keys_lock:
            keys = self._known_keys

        for key_name in keys:
            if key_name not in value:
                continue
            secret = self._get_secret(key_name)
            if secret is not None:
                value = value.replace(key_name, secret)
        return value

    def _replace_in_headers(self, flow: http.HTTPFlow) -> None:
        """Scan all header values and replace key names with real secrets."""
        with self._keys_lock:
            keys = self._known_keys
        if not keys:
            return

        for header_name in list(flow.request.headers.keys()):
            value = flow.request.headers[header_name]

            # Handle Basic auth: base64-decode, replace, re-encode
            if value.startswith("Basic "):
                try:
                    decoded = base64.b64decode(value[6:]).decode()
                except Exception:
                    continue
                has_key = any(k in decoded for k in keys)
                if not has_key:
                    continue
                replaced = self._replace_key_names(decoded)
                if replaced != decoded:
                    flow.request.headers[header_name] = (
                        "Basic " + base64.b64encode(replaced.encode()).decode()
                    )
                continue

            # Regular header value scan
            has_key = any(k in value for k in keys)
            if not has_key:
                continue
            replaced = self._replace_key_names(value)
            if replaced != value:
                flow.request.headers[header_name] = replaced

    # ------------------------------------------------------------------
    # Provider rewriting
    # ------------------------------------------------------------------

    def _try_provider_rewrite(self, flow: http.HTTPFlow, host: str) -> bool:
        """Rewrite amp provider proxy calls to go directly to the real API.

        Sets headers with key-name placeholders — the replacement logic
        resolves them afterward.

        Returns True if the request was rewritten.
        """
        if host not in ("ampcode.com", "api.ampcode.com"):
            return False

        path = flow.request.path
        for prefix, (real_host, header_name, header_value) in _PROVIDER_REWRITES.items():
            if not path.startswith(prefix):
                continue

            # Rewrite: /api/provider/anthropic/v1/messages → /v1/messages
            new_path = path[len(prefix) - 1 :]  # keep the leading /
            flow.request.host = real_host
            flow.request.port = 443
            flow.request.scheme = "https"
            flow.request.path = new_path
            flow.request.headers["host"] = real_host
            flow.request.headers[header_name] = header_value

            # Remove amp-specific auth header since we're going direct
            if header_name != "authorization" and "authorization" in flow.request.headers:
                del flow.request.headers["authorization"]

            log.info(
                "provider rewrite: %s%s → %s%s",
                host,
                path,
                real_host,
                new_path,
            )
            return True

        return False

    # ------------------------------------------------------------------
    # mitmproxy request hook
    # ------------------------------------------------------------------

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host.lower().rstrip(".")

        if host in BLOCKED_HOSTS:
            flow.response = http.Response.make(
                403,
                b"Blocked by security policy",
                {"content-type": "text/plain"},
            )
            log.warning("blocked request to %s", host)
            return

        # Check for amp provider proxy rewrite first
        self._try_provider_rewrite(flow, host)

        # Replace key names in ALL header values (including any just set
        # by provider rewrite above)
        self._replace_in_headers(flow)


addons = [CredentialInjector()]
