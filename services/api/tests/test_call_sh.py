"""Regression tests for the sandbox `call` helper's agent shortcut."""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CALL_SH = Path(__file__).resolve().parents[2] / "sandbox" / "call.sh"


class _AgentHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict]] = []
    headers_seen: list[dict[str, str]] = []

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        payload = json.loads(raw) if raw else {}
        self.__class__.requests.append(("POST", self.path, payload))
        self.__class__.headers_seen.append(dict(self.headers.items()))

        if self.path == "/agent/spawn":
            response = {"ok": True, "assignment_generation": 7}
            status = 200
        elif self.path == "/agent/message":
            response = {"ok": True, "message_id": payload.get("message_id")}
            status = 200
        elif self.path == "/agent/execute":
            response = {"ok": True, "execution_id": "exe-123", "status": "queued"}
            status = 202
        else:
            response = {"error": f"unexpected POST path {self.path}"}
            status = 404

        self._respond(status, response)

    def do_GET(self) -> None:  # noqa: N802
        self.__class__.requests.append(("GET", self.path, {}))
        self.__class__.headers_seen.append(dict(self.headers.items()))
        if self.path.startswith("/agent/runtime"):
            self._respond(
                200,
                {
                    "thread_key": "task:legal-review-123",
                    "persona_id": "legal",
                    "overlay": {"loaded": True},
                    "available_personas": ["eng", "legal"],
                },
            )
            return
        self._respond(404, {"error": f"unexpected GET path {self.path}"})

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _run_call(
    body: str, server: ThreadingHTTPServer
) -> subprocess.CompletedProcess[str]:
    return _run_call_args(["agent", "execute", body], server)


def _run_call_args(
    args: list[str],
    server: ThreadingHTTPServer,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "CENTAUR_API_URL": f"http://127.0.0.1:{server.server_port}",
        "CENTAUR_API_KEY": "test-token",
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(CALL_SH), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_call_agent_execute_uses_spawn_message_execute_flow():
    _AgentHandler.requests = []
    _AgentHandler.headers_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        result = _run_call(
            json.dumps(
                {
                    "thread_key": "task:legal-review-123",
                    "message": "Review this SAFE for risks",
                    "harness": "legal",
                }
            ),
            server,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout) == {
        "ok": True,
        "execution_id": "exe-123",
        "status": "queued",
    }

    assert [(method, path) for method, path, _ in _AgentHandler.requests] == [
        ("POST", "/agent/spawn"),
        ("POST", "/agent/message"),
        ("POST", "/agent/execute"),
    ]

    spawn_payload = _AgentHandler.requests[0][2]
    assert spawn_payload["thread_key"] == "task:legal-review-123"
    assert spawn_payload["harness"] == "legal"

    message_payload = _AgentHandler.requests[1][2]
    assert message_payload["thread_key"] == "task:legal-review-123"
    assert message_payload["assignment_generation"] == 7
    assert message_payload["role"] == "user"
    assert message_payload["parts"] == [
        {"type": "text", "text": "Review this SAFE for risks"}
    ]

    execute_payload = _AgentHandler.requests[2][2]
    assert execute_payload["thread_key"] == "task:legal-review-123"
    assert execute_payload["assignment_generation"] == 7
    assert execute_payload["harness"] == "legal"
    assert "message" not in execute_payload


def test_call_agent_execute_preserves_low_level_execute_payload():
    _AgentHandler.requests = []
    _AgentHandler.headers_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        result = _run_call(
            json.dumps(
                {
                    "thread_key": "task:raw-execute-123",
                    "assignment_generation": 5,
                    "execute_id": "exec-raw-123",
                    "harness": "amp",
                }
            ),
            server,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    assert [(method, path) for method, path, _ in _AgentHandler.requests] == [
        ("POST", "/agent/execute"),
    ]
    assert _AgentHandler.requests[0][2] == {
        "thread_key": "task:raw-execute-123",
        "assignment_generation": 5,
        "execute_id": "exec-raw-123",
        "harness": "amp",
    }


def test_call_agent_runtime_uses_get_with_query_string():
    _AgentHandler.requests = []
    _AgentHandler.headers_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        # The SYSTEM_PROMPT instructs agents to use exactly this shape; without
        # the dedicated `runtime` branch in call.sh this would fall through to
        # `request "POST" "$U/agent/runtime"` and 405 against the GET route.
        result = _run_call_args(
            ["agent", "runtime", "?key=task:legal-review-123"], server
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    assert [(method, path) for method, path, _ in _AgentHandler.requests] == [
        ("GET", "/agent/runtime?key=task:legal-review-123"),
    ]
    body = json.loads(result.stdout)
    assert body["persona_id"] == "legal"
    assert body["overlay"]["loaded"] is True


def test_call_discover_agent_lists_runtime_method():
    _AgentHandler.requests = []
    _AgentHandler.headers_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        result = _run_call_args(["discover", "agent"], server)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    body = json.loads(result.stdout)
    method_names = {entry["name"] for entry in body["methods"]}
    assert {"execute", "status", "runtime", "stop"} <= method_names


def test_call_uses_trace_id_header_and_separate_thread_key_header():
    _AgentHandler.requests = []
    _AgentHandler.headers_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        result = _run_call_args(
            [
                "agent",
                "execute",
                json.dumps(
                    {
                        "thread_key": "task:raw-execute-123",
                        "assignment_generation": 5,
                    }
                ),
            ],
            server,
            extra_env={
                "CENTAUR_TRACE_ID": "00000000-0000-0000-0000-000000000123",
                "CENTAUR_THREAD_KEY": "slack:C123:1700000000.000100",
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    headers = _AgentHandler.headers_seen[0]
    assert headers["X-Trace-Id"] == "00000000-0000-0000-0000-000000000123"
    assert headers["X-Centaur-Thread-Key"] == "slack:C123:1700000000.000100"


def test_call_bypasses_proxy_for_centaur_internal_hosts():
    _AgentHandler.requests = []
    _AgentHandler.headers_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        result = _run_call_args(
            ["agent", "runtime", "?key=task:legal-review-123"],
            server,
            extra_env={
                "http_proxy": "http://127.0.0.1:9",
                "https_proxy": "http://127.0.0.1:9",
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    assert [(method, path) for method, path, _ in _AgentHandler.requests] == [
        ("GET", "/agent/runtime?key=task:legal-review-123"),
    ]


# ---------------------------------------------------------------------------
# Generic `call <tool> <method> '<json>'` dispatch + local-tool routing.
# ---------------------------------------------------------------------------


class _ToolHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, str]] = []

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        self.__class__.requests.append(("POST", self.path, raw))
        self._send(b"sidecar-result")

    def do_GET(self) -> None:  # noqa: N802
        self.__class__.requests.append(("GET", self.path, ""))
        if self.path == "/tools":
            self._send(b'{"remotetool":{"description":"r","methods":["rm"]}}')
        else:
            self._send(b'{"sidecar":"discovery"}')


def _serve(handler_cls):
    handler_cls.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _bash_squote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _write_fake_runner(tmp_path: Path, *, stdout: str, exit_code: int) -> Path:
    runner = tmp_path / "centaur-tool"
    argv_file = tmp_path / "argv.txt"
    runner.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > {argv_file}\n'
        f"printf '%s' {_bash_squote(stdout)}\n"
        f"exit {exit_code}\n"
    )
    runner.chmod(0o755)
    return runner


def test_call_generic_tool_routes_to_tools_post():
    server, thread = _serve(_ToolHandler)
    try:
        result = _run_call_args(
            ["coingecko", "get_price", json.dumps({"ids": "bitcoin"})], server
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "sidecar-result"
    assert [(p, b) for _, p, b in _ToolHandler.requests] == [
        ("/tools/coingecko/get_price", json.dumps({"ids": "bitcoin"})),
    ]


def test_call_local_tool_runs_runner_and_skips_sidecar(tmp_path: Path):
    runner = _write_fake_runner(tmp_path, stdout='{"price":42}', exit_code=0)
    server, thread = _serve(_ToolHandler)
    try:
        result = _run_call_args(
            ["coingecko", "get_price", json.dumps({"ids": "bitcoin"})],
            server,
            extra_env={
                "CENTAUR_LOCAL_TOOLS": "coingecko",
                "CENTAUR_TOOL_BIN": str(runner),
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == '{"price":42}'
    assert _ToolHandler.requests == []
    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert argv == ["coingecko", "get_price", json.dumps({"ids": "bitcoin"})]


def test_call_local_tool_failure_propagates_without_sidecar(tmp_path: Path):
    # A local tool that fails is final: its envelope + non-zero exit are returned
    # verbatim and the sidecar is never contacted (no fallback).
    runner = _write_fake_runner(
        tmp_path, stdout='{"error":"boom","tool":"coingecko"}', exit_code=1
    )
    server, thread = _serve(_ToolHandler)
    try:
        result = _run_call_args(
            ["coingecko", "get_price", json.dumps({"ids": "bitcoin"})],
            server,
            extra_env={
                "CENTAUR_LOCAL_TOOLS": "coingecko",
                "CENTAUR_TOOL_BIN": str(runner),
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 1
    assert result.stdout.strip() == '{"error":"boom","tool":"coingecko"}'
    assert _ToolHandler.requests == []


def test_call_non_allowlisted_tool_uses_sidecar(tmp_path: Path):
    runner = _write_fake_runner(tmp_path, stdout="LOCAL", exit_code=0)
    server, thread = _serve(_ToolHandler)
    try:
        result = _run_call_args(
            ["coingecko", "get_price", json.dumps({"ids": "bitcoin"})],
            server,
            extra_env={
                "CENTAUR_LOCAL_TOOLS": "defillama",
                "CENTAUR_TOOL_BIN": str(runner),
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "sidecar-result"
    assert not (tmp_path / "argv.txt").exists()
    assert len(_ToolHandler.requests) == 1


def test_call_missing_runner_binary_fails_without_sidecar(tmp_path: Path):
    # A misconfigured local route (no runner binary) fails rather than silently
    # falling back to the sidecar.
    server, thread = _serve(_ToolHandler)
    try:
        result = _run_call_args(
            ["coingecko", "get_price", json.dumps({"ids": "bitcoin"})],
            server,
            extra_env={
                "CENTAUR_LOCAL_TOOLS": "all",
                "CENTAUR_TOOL_BIN": str(tmp_path / "does-not-exist"),
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode != 0
    assert _ToolHandler.requests == []


def test_call_discover_local_tool_uses_runner(tmp_path: Path):
    # `call discover <tool>` for a local tool is described from the baked tool
    # code via the runner — the tool server is not contacted.
    describe = '{"tool":"coingecko","description":"cg","methods":[]}'
    runner = _write_fake_runner(tmp_path, stdout=describe, exit_code=0)
    server, thread = _serve(_ToolHandler)
    try:
        result = _run_call_args(
            ["discover", "coingecko"],
            server,
            extra_env={
                "CENTAUR_LOCAL_TOOLS": "coingecko",
                "CENTAUR_TOOL_BIN": str(runner),
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == describe
    assert _ToolHandler.requests == []
    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert argv == ["__describe", "coingecko"]


def test_call_discover_remote_tool_uses_sidecar(tmp_path: Path):
    runner = _write_fake_runner(tmp_path, stdout="LOCAL", exit_code=0)
    server, thread = _serve(_ToolHandler)
    try:
        result = _run_call_args(
            ["discover", "coingecko"],
            server,
            extra_env={
                "CENTAUR_LOCAL_TOOLS": "defillama",  # coingecko stays remote
                "CENTAUR_TOOL_BIN": str(runner),
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    assert not (tmp_path / "argv.txt").exists()
    assert [(m, p) for m, p, _ in _ToolHandler.requests] == [
        ("GET", "/tools/coingecko"),
    ]


def test_call_tools_all_local_lists_from_runner(tmp_path: Path):
    # With CENTAUR_LOCAL_TOOLS=all, `call tools` lists from the runner and still
    # injects the built-in agent sub-command — no tool-server round trip.
    listing = '{"coingecko":{"description":"cg","methods":["get_price"]}}'
    runner = _write_fake_runner(tmp_path, stdout=listing, exit_code=0)
    server, thread = _serve(_ToolHandler)
    try:
        result = _run_call_args(
            ["tools"],
            server,
            extra_env={
                "CENTAUR_LOCAL_TOOLS": "all",
                "CENTAUR_TOOL_BIN": str(runner),
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    body = json.loads(result.stdout)
    assert "coingecko" in body
    assert body["agent"]["methods"] == ["execute", "status", "runtime", "stop"]
    assert _ToolHandler.requests == []
    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert argv == ["__list"]


def test_call_tools_mixed_unions_local_and_sidecar(tmp_path: Path):
    # In mixed state call.sh owns the union: local tools (from the runner) folded
    # together with the tool server's tools, local winning on overlap.
    listing = '{"coingecko":{"description":"local cg","methods":["get_price"]}}'
    runner = _write_fake_runner(tmp_path, stdout=listing, exit_code=0)
    server, thread = _serve(_ToolHandler)
    try:
        result = _run_call_args(
            ["tools"],
            server,
            extra_env={
                "CENTAUR_LOCAL_TOOLS": "coingecko",  # not "all"
                "CENTAUR_TOOL_BIN": str(runner),
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr or result.stdout
    body = json.loads(result.stdout)
    # both sources unioned, plus the built-in agent
    assert body["remotetool"]["methods"] == ["rm"]
    assert body["coingecko"]["description"] == "local cg"
    assert "agent" in body
    assert (tmp_path / "argv.txt").read_text().splitlines() == ["__list"]
    assert [(m, p) for m, p, _ in _ToolHandler.requests] == [("GET", "/tools")]


def test_call_tools_sidecar_down_still_lists_local(tmp_path: Path):
    # If the tool server is unreachable, the union still returns local tools.
    listing = '{"coingecko":{"description":"local cg","methods":["get_price"]}}'
    runner = _write_fake_runner(tmp_path, stdout=listing, exit_code=0)
    # Point CENTAUR_API_URL/TU at a dead port so the GET fails fast.
    result = subprocess.run(
        ["bash", str(CALL_SH), "tools"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "CENTAUR_API_URL": "http://127.0.0.1:1",
            "CENTAUR_API_KEY": "test-token",
            "CENTAUR_LOCAL_TOOLS": "coingecko",
            "CENTAUR_TOOL_BIN": str(runner),
            "CALL_TIMEOUT_SECONDS": "2",
        },
    )
    assert result.returncode == 0, result.stderr or result.stdout
    body = json.loads(result.stdout)
    assert body["coingecko"]["description"] == "local cg"
    assert "agent" in body
