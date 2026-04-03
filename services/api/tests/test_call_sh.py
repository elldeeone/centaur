"""Regression tests for the sandbox `call` helper's agent shortcut."""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CALL_SH = Path(__file__).resolve().parents[2] / "sandbox" / "call.sh"


class _AgentHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict]] = []

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        payload = json.loads(raw) if raw else {}
        self.__class__.requests.append((self.path, payload))

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
            response = {"error": f"unexpected path {self.path}"}
            status = 404

        body = json.dumps(response).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _run_call(body: str, server: ThreadingHTTPServer) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(CALL_SH), "agent", "execute", body],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "CENTAUR_API_URL": f"http://127.0.0.1:{server.server_port}",
            "CENTAUR_API_KEY": "test-token",
        },
    )


def test_call_agent_execute_uses_spawn_message_execute_flow():
    _AgentHandler.requests = []
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
    assert json.loads(result.stdout) == {"ok": True, "execution_id": "exe-123", "status": "queued"}

    assert [path for path, _ in _AgentHandler.requests] == [
        "/agent/spawn",
        "/agent/message",
        "/agent/execute",
    ]

    spawn_payload = _AgentHandler.requests[0][1]
    assert spawn_payload["thread_key"] == "task:legal-review-123"
    assert spawn_payload["harness"] == "legal"

    message_payload = _AgentHandler.requests[1][1]
    assert message_payload["thread_key"] == "task:legal-review-123"
    assert message_payload["assignment_generation"] == 7
    assert message_payload["role"] == "user"
    assert message_payload["parts"] == [{"type": "text", "text": "Review this SAFE for risks"}]

    execute_payload = _AgentHandler.requests[2][1]
    assert execute_payload["thread_key"] == "task:legal-review-123"
    assert execute_payload["assignment_generation"] == 7
    assert execute_payload["harness"] == "legal"
    assert "message" not in execute_payload


def test_call_agent_execute_preserves_low_level_execute_payload():
    _AgentHandler.requests = []
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
    assert [path for path, _ in _AgentHandler.requests] == ["/agent/execute"]
    assert _AgentHandler.requests[0][1] == {
        "thread_key": "task:raw-execute-123",
        "assignment_generation": 5,
        "execute_id": "exec-raw-123",
        "harness": "amp",
    }
