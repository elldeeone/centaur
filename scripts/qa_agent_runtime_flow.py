#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS_MARKER = "__CENTAUR_QA_HTTP_STATUS__:"
DEFAULT_EXTERNAL_URL = os.environ.get("QA_AGENT_RUNTIME_API_URL", "http://localhost:8000")
DEFAULT_API_CONTAINER = os.environ.get("QA_AGENT_RUNTIME_API_CONTAINER", "centaur-api-1")
DEFAULT_READY_TIMEOUT_S = float(os.environ.get("QA_AGENT_RUNTIME_READY_TIMEOUT_S", "90"))
DEFAULT_EXECUTION_TIMEOUT_S = float(os.environ.get("QA_AGENT_RUNTIME_EXECUTION_TIMEOUT_S", "180"))
DEFAULT_EVENT_TIMEOUT_S = float(os.environ.get("QA_AGENT_RUNTIME_EVENT_TIMEOUT_S", "2.0"))
DEFAULT_EVENT_POLL_MS = int(os.environ.get("QA_AGENT_RUNTIME_EVENT_POLL_MS", "100"))
DEFAULT_POLL_INTERVAL_S = float(os.environ.get("QA_AGENT_RUNTIME_POLL_INTERVAL_S", "1.0"))
DEFAULT_REQUEST_TIMEOUT_S = float(os.environ.get("QA_AGENT_RUNTIME_REQUEST_TIMEOUT_S", "60"))
DEFAULT_EXTERNAL_API_KEY_ENV_KEYS = (
    "QA_AGENT_RUNTIME_API_KEY",
    "LOCAL_DEV_API_KEY",
    "SLACKBOT_API_KEY",
)


def _load_repo_dotenv() -> dict[str, str]:
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


def _resolve_external_api_key() -> tuple[str, str] | None:
    dotenv = _load_repo_dotenv()
    for env_var in DEFAULT_EXTERNAL_API_KEY_ENV_KEYS:
        token = os.environ.get(env_var, "").strip() or dotenv.get(env_var, "").strip()
        if token:
            return env_var, token
    return None


@dataclass
class HttpResponse:
    status: int | None
    body: Any
    headers: dict[str, str]
    body_text: str


class QARunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.api_url = args.api_url.rstrip("/")
        self.api_container = args.api_container
        self.ready_timeout_s = args.ready_timeout_s
        self.execution_timeout_s = args.execution_timeout_s
        self.event_timeout_s = args.event_timeout_s
        self.event_poll_ms = args.event_poll_ms
        self.poll_interval_s = args.poll_interval_s
        self.request_timeout_s = args.request_timeout_s
        self.with_agent_tool_smoke = args.with_agent_tool_smoke
        self.api_key: str | None = None
        self.api_key_id: str | None = None
        self.api_key_source: str | None = None
        self.created_threads: list[str] = []
        self.cleanup_errors: list[str] = []
        self.summary: dict[str, Any] = {
            "ok": False,
            "api_url": self.api_url,
            "started_at": int(time.time()),
            "auth_paths": {
                "external": self.api_url,
                "sandbox_internal": "http://api:8000",
            },
            "scenarios": {},
        }

    def fail(self, message: str) -> None:
        raise AssertionError(message)

    def expect(self, condition: bool, message: str) -> None:
        if not condition:
            self.fail(message)

    def thread_key(self, label: str) -> str:
        return f"qa:{label}:{int(time.time())}-{uuid.uuid4().hex[:6]}"

    def track_thread(self, key: str) -> str:
        if key not in self.created_threads:
            self.created_threads.append(key)
        return key

    def encode_path(self, value: str) -> str:
        return urllib.parse.quote(value, safe="")

    def run_command(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        timeout_s: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_s,
            cwd=REPO_ROOT,
        )

    def decode_response_body(self, raw: bytes, content_type: str) -> Any:
        if not raw:
            return None
        if "application/json" in content_type:
            return json.loads(raw.decode("utf-8"))
        with contextlib.suppress(UnicodeDecodeError):
            return raw.decode("utf-8")
        return raw

    def external_request(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        *,
        use_auth: bool = True,
    ) -> HttpResponse:
        url = f"{self.api_url}{path}"
        headers: dict[str, str] = {}
        data: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        if use_auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout_s) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                return HttpResponse(
                    status=response.status,
                    body=self.decode_response_body(raw, content_type),
                    headers=dict(response.headers.items()),
                    body_text=raw.decode("utf-8", errors="replace") if raw else "",
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            content_type = exc.headers.get("Content-Type", "")
            return HttpResponse(
                status=exc.code,
                body=self.decode_response_body(raw, content_type),
                headers=dict(exc.headers.items()),
                body_text=raw.decode("utf-8", errors="replace") if raw else "",
            )

    def admin_request(self, method: str, path: str, payload: Any | None = None) -> HttpResponse:
        url = f"http://localhost:8000{path}"
        cmd = ["docker", "exec"]
        input_text: str | None = None
        if payload is not None:
            input_text = json.dumps(payload)
            cmd.append("-i")
        cmd.append(self.api_container)
        cmd.extend(
            [
                "curl",
                "-sS",
                "--max-time",
                str(self.request_timeout_s),
                "-X",
                method,
                "-o",
                "-",
                "-w",
                f"\n{STATUS_MARKER}%{{http_code}}",
                url,
            ]
        )
        if payload is not None:
            cmd.extend(["-H", "Content-Type: application/json", "--data-binary", "@-"])
        proc = self.run_command(cmd, input_text=input_text, timeout_s=self.request_timeout_s + 5)
        if proc.returncode != 0:
            return HttpResponse(
                status=None,
                body=None,
                headers={},
                body_text=(proc.stderr or proc.stdout or "admin curl failed").strip(),
            )
        stdout = proc.stdout
        if STATUS_MARKER not in stdout:
            return HttpResponse(status=None, body=None, headers={}, body_text=stdout.strip())
        raw_body, _, raw_status = stdout.rpartition(f"\n{STATUS_MARKER}")
        try:
            status = int(raw_status.strip())
        except ValueError:
            status = None
        body_text = raw_body.strip()
        body: Any
        if body_text:
            try:
                body = json.loads(body_text)
            except json.JSONDecodeError:
                body = body_text
        else:
            body = None
        return HttpResponse(status=status, body=body, headers={}, body_text=body_text)

    def get_json(self, path: str, expected_status: int = 200, *, use_auth: bool = True) -> Any:
        response = self.external_request("GET", path, use_auth=use_auth)
        self.expect(
            response.status == expected_status,
            f"GET {path} returned {response.status}: {response.body}",
        )
        return response.body

    def get_bytes(self, path: str, expected_status: int = 200) -> bytes:
        response = self.external_request("GET", path)
        self.expect(
            response.status == expected_status,
            f"GET {path} returned {response.status}: {response.body}",
        )
        if isinstance(response.body, bytes):
            return response.body
        if isinstance(response.body, str):
            return response.body.encode("utf-8")
        self.fail(f"GET {path} did not return bytes: {response.body!r}")

    def post_json(self, path: str, payload: Any, expected_status: int) -> Any:
        response = self.external_request("POST", path, payload)
        self.expect(
            response.status == expected_status,
            f"POST {path} returned {response.status}: {response.body}",
        )
        return response.body

    def wait_external_health(self) -> dict[str, Any]:
        deadline = time.time() + self.ready_timeout_s
        last_error: str | None = None
        while time.time() < deadline:
            try:
                body = self.get_json("/health", expected_status=200, use_auth=False)
                self.expect(body.get("status") == "ok", f"readyz returned unexpected body: {body}")
                return body
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                time.sleep(1)
        self.fail(f"external health did not become ready within {self.ready_timeout_s}s: {last_error}")

    def wait_authenticated_agent_api(self) -> dict[str, Any]:
        deadline = time.time() + self.ready_timeout_s
        last_error: str | None = None
        while time.time() < deadline:
            try:
                body = self.get_json("/agent/threads?limit=1", expected_status=200)
                self.expect(isinstance(body, dict) and "threads" in body, f"agent threads returned unexpected body: {body}")
                return body
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                time.sleep(1)
        self.fail(
            f"authenticated agent api did not become ready within {self.ready_timeout_s}s: {last_error}"
        )

    def mint_external_api_key(self) -> dict[str, Any]:
        provided = _resolve_external_api_key()
        if provided is not None:
            source, token = provided
            self.api_key = token
            self.api_key_source = source
            return {
                "id": f"env:{source}",
                "name": f"env:{source}",
                "key_prefix": token[:8],
                "scopes": ["provided"],
                "source": source,
            }

        payload = {
            "name": f"qa-agent-runtime-{uuid.uuid4().hex[:8]}",
            "scopes": ["agent"],
            "created_by": "scripts/qa_agent_runtime_flow.py",
        }
        response = self.admin_request("POST", "/admin/api-keys", payload)
        self.expect(response.status == 200, f"mint api key failed: {response.status} {response.body}")
        self.expect(isinstance(response.body, dict), f"mint api key returned non-json: {response.body}")
        key = response.body.get("key")
        key_id = response.body.get("id")
        self.expect(isinstance(key, str) and key, f"mint api key missing key: {response.body}")
        self.expect(isinstance(key_id, str) and key_id, f"mint api key missing id: {response.body}")
        self.api_key = key
        self.api_key_id = key_id
        self.api_key_source = "admin-mint"
        return {
            "id": key_id,
            "name": response.body.get("name"),
            "key_prefix": response.body.get("key_prefix"),
            "scopes": response.body.get("scopes"),
            "source": "admin-mint",
        }

    def revoke_external_api_key(self) -> None:
        if not self.api_key_id:
            return
        response = self.admin_request("DELETE", f"/admin/api-keys/{self.api_key_id}")
        self.expect(response.status in {200, 404}, f"revoke api key failed: {response.status} {response.body}")

    def parse_sse(self, text: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        data_lines: list[str] = []

        def flush() -> None:
            nonlocal current, data_lines
            if not current and not data_lines:
                return
            event = dict(current)
            if data_lines:
                raw_data = "\n".join(data_lines)
                event["raw_data"] = raw_data
                with contextlib.suppress(json.JSONDecodeError):
                    event["payload"] = json.loads(raw_data)
            if "id" in event:
                with contextlib.suppress(ValueError):
                    event["id"] = int(event["id"])
            events.append(event)
            current = {}
            data_lines = []

        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                flush()
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
            else:
                current[field] = value
        flush()
        return events

    def fetch_events(
        self,
        key: str,
        execution_id: str,
        *,
        after_event_id: int = 0,
        max_time_s: float | None = None,
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "execution_id": execution_id,
                "after_event_id": after_event_id,
                "poll_ms": self.event_poll_ms,
            }
        )
        url = f"{self.api_url}/agent/threads/{self.encode_path(key)}/events?{query}"
        cmd = [
            "curl",
            "-sN",
            "--max-time",
            f"{max_time_s or self.event_timeout_s:g}",
            "-H",
            f"Authorization: Bearer {self.api_key}",
            url,
        ]
        proc = self.run_command(cmd, timeout_s=(max_time_s or self.event_timeout_s) + 5)
        self.expect(
            proc.returncode in {0, 28},
            f"curl failed while streaming events (exit {proc.returncode}): {proc.stderr.strip()}",
        )
        return self.parse_sse(proc.stdout)

    def latest_event_id(self, events: list[dict[str, Any]]) -> int:
        ids = [int(event["id"]) for event in events if isinstance(event.get("id"), int)]
        return max(ids) if ids else 0

    def wait_for_terminal(self, execution_id: str) -> dict[str, Any]:
        deadline = time.time() + self.execution_timeout_s
        last_status: dict[str, Any] | None = None
        non_terminal_statuses = {"queued", "running", "cancel_requested", "retry_wait"}
        while time.time() < deadline:
            status = self.get_json(f"/agent/executions/{execution_id}")
            last_status = status
            if status.get("status") not in non_terminal_statuses:
                return status
            time.sleep(self.poll_interval_s)
        self.fail(
            f"execution {execution_id} did not finish within {self.execution_timeout_s}s (last={last_status})"
        )

    def release_thread(self, key: str) -> None:
        self.post_json(
            f"/agent/threads/{self.encode_path(key)}/release",
            {"release_id": f"rel-{uuid.uuid4().hex[:8]}", "cancel_inflight": True},
            expected_status=200,
        )

    def mark_final_delivered(
        self,
        key: str,
        execution_id: str,
        last_event_id: int,
    ) -> list[dict[str, Any]]:
        self.post_json(f"/agent/final-deliveries/{execution_id}/delivered", {}, expected_status=200)
        delivered_events = self.fetch_events(
            key,
            execution_id,
            after_event_id=last_event_id,
            max_time_s=1.5,
        )
        self.expect(
            any(event.get("event") == "final_delivery_delivered" for event in delivered_events),
            f"execution {execution_id} did not emit final_delivery_delivered after ack",
        )
        return delivered_events

    def cancel_execution(self, execution_id: str) -> dict[str, Any]:
        return self.post_json(
            f"/agent/executions/{execution_id}/cancel",
            {"reason": "qa_cancel"},
            expected_status=200,
        )

    def amp_payloads(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for event in events:
            if event.get("event") != "amp_raw_event":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict):
                payloads.append(payload)
        return payloads

    def find_turn_done(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        for payload in self.amp_payloads(events):
            if payload.get("type") == "turn.done":
                return payload
        return None

    def find_execution_state(
        self,
        events: list[dict[str, Any]],
        *,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        for event in events:
            if event.get("event") != "execution_state":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if status is None or payload.get("status") == status:
                return payload
        return None

    def has_thinking_block(self, events: list[dict[str, Any]]) -> bool:
        for payload in self.amp_payloads(events):
            if payload.get("type") != "assistant":
                continue
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "thinking":
                    return True
        return False

    def has_tool_use(self, events: list[dict[str, Any]]) -> bool:
        for payload in self.amp_payloads(events):
            if payload.get("type") != "assistant":
                continue
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    return True
        return False

    def has_tool_result(self, events: list[dict[str, Any]]) -> bool:
        for payload in self.amp_payloads(events):
            if payload.get("type") == "tool":
                return True
            if payload.get("type") != "user":
                continue
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
        return False

    def assistant_texts(self, events: list[dict[str, Any]]) -> list[str]:
        texts: list[str] = []
        for payload in self.amp_payloads(events):
            if payload.get("type") != "assistant":
                continue
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
        return texts

    def raw_event_contains(self, events: list[dict[str, Any]], needle: str) -> bool:
        return any(needle in event.get("raw_data", "") for event in events)

    def ensure_final_delivery_ready(self, events: list[dict[str, Any]], execution_id: str) -> None:
        self.expect(
            any(event.get("event") == "final_delivery_ready" for event in events),
            f"execution {execution_id} did not emit final_delivery_ready",
        )

    def run_negative_checks(self) -> dict[str, Any]:
        key = self.thread_key("seal")
        connect = self.external_request(
            "POST",
            "/agent/connect",
            {"thread_key": key},
        )
        self.expect(connect.status == 410, f"legacy connect expected 410, got {connect.status}: {connect.body}")
        self.expect(
            isinstance(connect.body, dict) and connect.body.get("code") == "LEGACY_ENDPOINT_REMOVED",
            f"unexpected connect body: {connect.body}",
        )
        reconnect = self.external_request(
            "POST",
            "/agent/reconnect",
            {"thread_key": key},
        )
        self.expect(reconnect.status == 410, f"legacy reconnect expected 410, got {reconnect.status}: {reconnect.body}")
        self.expect(
            isinstance(reconnect.body, dict) and reconnect.body.get("code") == "LEGACY_ENDPOINT_REMOVED",
            f"unexpected reconnect body: {reconnect.body}",
        )

        message = self.external_request(
            "POST",
            "/agent/message",
            {
                "thread_key": key,
                "assignment_generation": 1,
                "role": "user",
                "parts": [{"type": "text", "text": "hello"}],
            },
        )
        self.expect(message.status == 409, f"message without spawn expected 409, got {message.status}: {message.body}")
        self.expect(
            isinstance(message.body, dict) and message.body.get("code") == "NO_ACTIVE_ASSIGNMENT",
            f"unexpected message body: {message.body}",
        )

        track_key = self.track_thread(key)
        spawn = self.post_json("/agent/spawn", {"thread_key": track_key, "harness": "amp"}, expected_status=200)
        stale = self.external_request(
            "POST",
            "/agent/execute",
            {
                "thread_key": track_key,
                "assignment_generation": spawn["assignment_generation"] - 1,
                "execute_id": f"exec-stale-{uuid.uuid4().hex[:8]}",
                "delivery": {"platform": "qa"},
            },
        )
        self.expect(stale.status == 409, f"stale execute expected 409, got {stale.status}: {stale.body}")
        self.expect(
            isinstance(stale.body, dict) and stale.body.get("code") == "ASSIGNMENT_GENERATION_STALE",
            f"unexpected stale body: {stale.body}",
        )
        return {
            "legacy_connect": {"status": connect.status, "code": connect.body["code"]},
            "legacy_reconnect": {"status": reconnect.status, "code": reconnect.body["code"]},
            "message_without_spawn": {"status": message.status, "code": message.body["code"]},
            "execute_stale_generation": {"status": stale.status, "code": stale.body["code"]},
        }

    def run_normal_prompt(self) -> dict[str, Any]:
        key = self.track_thread(self.thread_key("normal"))
        spawn = self.post_json("/agent/spawn", {"thread_key": key, "harness": "amp"}, expected_status=200)
        message = self.post_json(
            "/agent/message",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "role": "user",
                "parts": [{"type": "text", "text": "Reply with exactly PONG and nothing else."}],
            },
            expected_status=200,
        )
        execute = self.post_json(
            "/agent/execute",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "execute_id": f"exec-normal-{uuid.uuid4().hex[:8]}",
                "harness": "amp",
                "delivery": {"platform": "qa"},
            },
            expected_status=202,
        )
        status = self.wait_for_terminal(execute["execution_id"])
        self.expect(status.get("status") == "completed", f"normal prompt did not complete: {status}")
        self.expect(status.get("result_text") == "PONG", f"normal prompt result mismatch: {status}")
        events = self.fetch_events(key, execute["execution_id"], after_event_id=0, max_time_s=1.5)
        self.expect(events, "normal prompt produced no events")
        self.expect(self.has_thinking_block(events), "normal prompt lost Amp thinking blocks")
        turn_done = self.find_turn_done(events)
        self.expect(turn_done is not None, "normal prompt missing turn.done")
        self.expect(turn_done.get("result") == "PONG", f"normal turn.done mismatch: {turn_done}")
        self.ensure_final_delivery_ready(events, execute["execution_id"])
        delivered_events = self.mark_final_delivered(key, execute["execution_id"], self.latest_event_id(events))
        return {
            "thread_key": key,
            "execution_id": execute["execution_id"],
            "status": status["status"],
            "result_text": status["result_text"],
            "message_id": message["message_id"],
            "event_kinds": [event.get("event") for event in events],
            "delivered_event_kinds": [event.get("event") for event in delivered_events],
        }

    def run_reconnect_flow(self) -> dict[str, Any]:
        key = self.track_thread(self.thread_key("reconnect"))
        spawn = self.post_json("/agent/spawn", {"thread_key": key, "harness": "amp"}, expected_status=200)
        prompt = (
            "Use the shell_command tool to run `sleep 3; printf PONG`. "
            "After it completes, reply with exactly PONG and nothing else."
        )
        self.post_json(
            "/agent/message",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "role": "user",
                "parts": [{"type": "text", "text": prompt}],
            },
            expected_status=200,
        )
        execute = self.post_json(
            "/agent/execute",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "execute_id": f"exec-reconnect-{uuid.uuid4().hex[:8]}",
                "harness": "amp",
                "delivery": {"platform": "qa"},
            },
            expected_status=202,
        )
        early_events = self.fetch_events(key, execute["execution_id"], after_event_id=0, max_time_s=1.5)
        self.expect(early_events, "reconnect scenario produced no early events")
        early_last_id = self.latest_event_id(early_events)
        status = self.wait_for_terminal(execute["execution_id"])
        self.expect(status.get("status") == "completed", f"reconnect execution did not complete: {status}")
        self.expect(status.get("result_text") == "PONG", f"reconnect execution result mismatch: {status}")
        resumed_events = self.fetch_events(
            key,
            execute["execution_id"],
            after_event_id=early_last_id,
            max_time_s=2.5,
        )
        self.expect(resumed_events, "reconnect resume returned no events")
        self.expect(self.has_tool_use(resumed_events), "reconnect resume lost tool_use event")
        self.expect(self.has_tool_result(resumed_events), "reconnect resume lost tool_result event")
        self.expect("PONG" in self.assistant_texts(resumed_events), f"reconnect resume missing assistant text: {resumed_events}")
        turn_done = self.find_turn_done(resumed_events)
        self.expect(turn_done is not None, "reconnect resume missing turn.done")
        self.expect(turn_done.get("result") == "PONG", f"reconnect turn.done mismatch: {turn_done}")
        self.ensure_final_delivery_ready(resumed_events, execute["execution_id"])
        delivered_events = self.mark_final_delivered(key, execute["execution_id"], self.latest_event_id(resumed_events))
        return {
            "thread_key": key,
            "execution_id": execute["execution_id"],
            "status": status["status"],
            "result_text": status["result_text"],
            "after_event_id": early_last_id,
            "early_event_kinds": [event.get("event") for event in early_events],
            "resumed_event_kinds": [event.get("event") for event in resumed_events],
            "delivered_event_kinds": [event.get("event") for event in delivered_events],
        }

    def run_cancel_reconnect_flow(self) -> dict[str, Any]:
        key = self.track_thread(self.thread_key("cancel-reconnect"))
        spawn = self.post_json("/agent/spawn", {"thread_key": key, "harness": "amp"}, expected_status=200)
        cancel_prompt = (
            "Use the shell_command tool to run `sleep 15; printf SHOULD-NOT-HAPPEN`. "
            "After it completes, reply with exactly SHOULD-NOT-HAPPEN and nothing else."
        )
        self.post_json(
            "/agent/message",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "role": "user",
                "parts": [{"type": "text", "text": cancel_prompt}],
            },
            expected_status=200,
        )
        execute = self.post_json(
            "/agent/execute",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "execute_id": f"exec-cancel-reconnect-{uuid.uuid4().hex[:8]}",
                "harness": "amp",
                "delivery": {"platform": "qa"},
            },
            expected_status=202,
        )
        early_events = self.fetch_events(key, execute["execution_id"], after_event_id=0, max_time_s=5.0)
        self.expect(early_events, "cancel/reconnect scenario produced no early events")
        early_last_id = self.latest_event_id(early_events)
        running_state = self.find_execution_state(early_events, status="running")
        self.expect(
            running_state is not None,
            f"cancel/reconnect scenario never reached running state: {early_events}",
        )
        saw_amp_output = any(event.get("event") == "amp_raw_event" for event in early_events)
        cancel_result = self.cancel_execution(execute["execution_id"])
        self.expect(
            cancel_result.get("status") == "cancel_requested",
            f"cancel request returned unexpected body: {cancel_result}",
        )
        cancelled_events = self.fetch_events(
            key,
            execute["execution_id"],
            after_event_id=early_last_id,
            max_time_s=2.0,
        )
        cancel_requested_state = self.find_execution_state(cancelled_events, status="cancel_requested")
        cancelled_state = self.find_execution_state(cancelled_events, status="cancelled")
        self.expect(
            cancel_requested_state is not None or cancelled_state is not None,
            f"cancel/reconnect scenario missing cancel execution.state: {cancelled_events}",
        )
        cancelled_status = self.wait_for_terminal(execute["execution_id"])
        self.expect(
            cancelled_status.get("status") == "cancelled",
            f"cancel/reconnect execution did not cancel: {cancelled_status}",
        )
        if cancelled_state is None:
            late_cancelled_events = self.fetch_events(
                key,
                execute["execution_id"],
                after_event_id=self.latest_event_id(cancelled_events),
                max_time_s=1.5,
            )
            cancelled_events.extend(late_cancelled_events)
            cancelled_state = self.find_execution_state(cancelled_events, status="cancelled")
        self.expect(
            cancelled_state is not None,
            f"cancel/reconnect scenario never emitted terminal cancelled state: {cancelled_events}",
        )

        self.post_json(
            "/agent/message",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "role": "user",
                "parts": [
                    {"type": "text", "text": "Reply with exactly POST-CANCEL-RECOVERED and nothing else."}
                ],
            },
            expected_status=200,
        )
        follow_execute = self.post_json(
            "/agent/execute",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "execute_id": f"exec-post-cancel-{uuid.uuid4().hex[:8]}",
                "harness": "amp",
                "delivery": {"platform": "qa"},
            },
            expected_status=202,
        )
        follow_status = self.wait_for_terminal(follow_execute["execution_id"])
        self.expect(
            follow_status.get("status") == "completed",
            f"post-cancel follow-up did not complete: {follow_status}",
        )
        self.expect(
            follow_status.get("result_text") == "POST-CANCEL-RECOVERED",
            f"post-cancel follow-up returned stale output: {follow_status}",
        )
        follow_events = self.fetch_events(
            key,
            follow_execute["execution_id"],
            after_event_id=0,
            max_time_s=2.5,
        )
        self.expect(follow_events, "post-cancel follow-up produced no events")
        follow_turn_done = self.find_turn_done(follow_events)
        self.expect(follow_turn_done is not None, "post-cancel follow-up missing turn.done")
        self.expect(
            follow_turn_done.get("result") == "POST-CANCEL-RECOVERED",
            f"post-cancel follow-up turn.done mismatch: {follow_turn_done}",
        )
        self.ensure_final_delivery_ready(follow_events, follow_execute["execution_id"])
        delivered_events = self.mark_final_delivered(
            key,
            follow_execute["execution_id"],
            self.latest_event_id(follow_events),
        )
        return {
            "thread_key": key,
            "cancelled_execution_id": execute["execution_id"],
            "cancel_after_event_id": early_last_id,
            "saw_amp_output_before_cancel": saw_amp_output,
            "cancel_status": cancelled_status["status"],
            "follow_execution_id": follow_execute["execution_id"],
            "follow_status": follow_status["status"],
            "follow_result_text": follow_status["result_text"],
            "cancelled_event_kinds": [event.get("event") for event in cancelled_events],
            "follow_event_kinds": [event.get("event") for event in follow_events],
            "delivered_event_kinds": [event.get("event") for event in delivered_events],
        }

    def run_persona(self, persona_id: str) -> dict[str, Any]:
        key = self.track_thread(self.thread_key(f"persona:{persona_id}"))
        spawn = self.post_json(
            "/agent/spawn",
            {"thread_key": key, "harness": "amp", "persona_id": persona_id},
            expected_status=200,
        )
        self.expect(spawn.get("persona_id") == persona_id, f"spawn persona mismatch: {spawn}")
        prompt = (
            "Use the shell_command tool to print the AGENT_PERSONA environment variable with "
            "`printf \"%s\" \"$AGENT_PERSONA\"`. Then reply with exactly that value and nothing else."
        )
        self.post_json(
            "/agent/message",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "role": "user",
                "parts": [{"type": "text", "text": prompt}],
            },
            expected_status=200,
        )
        execute = self.post_json(
            "/agent/execute",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "execute_id": f"exec-persona-{persona_id}-{uuid.uuid4().hex[:8]}",
                "harness": "amp",
                "delivery": {"platform": "qa"},
            },
            expected_status=202,
        )
        status = self.wait_for_terminal(execute["execution_id"])
        self.expect(status.get("status") == "completed", f"persona {persona_id} did not complete: {status}")
        self.expect(status.get("result_text") == persona_id, f"persona {persona_id} result mismatch: {status}")
        events = self.fetch_events(key, execute["execution_id"], after_event_id=0, max_time_s=1.5)
        self.ensure_final_delivery_ready(events, execute["execution_id"])
        delivered_events = self.mark_final_delivered(key, execute["execution_id"], self.latest_event_id(events))
        return {
            "persona_id": persona_id,
            "thread_key": key,
            "execution_id": execute["execution_id"],
            "status": status["status"],
            "result_text": status["result_text"],
            "prompt_ref": spawn.get("prompt_ref"),
            "delivered_event_kinds": [event.get("event") for event in delivered_events],
        }

    def run_attachment_flow(self) -> dict[str, Any]:
        key = self.track_thread(self.thread_key("attachment"))
        expected_text = f"ATTACHMENT_OK_{int(time.time())}"
        spawn = self.post_json("/agent/spawn", {"thread_key": key, "harness": "amp"}, expected_status=200)
        message = self.post_json(
            "/agent/message",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "message_id": f"msg-attachment-{uuid.uuid4().hex[:8]}",
                "event": {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Download the attached file using the provided curl instruction, "
                                    "then reply with exactly the file contents and nothing else."
                                ),
                            },
                            {
                                "type": "document",
                                "source_path": "file:///tmp/qa-note.txt",
                                "source": {
                                    "type": "base64",
                                    "media_type": "text/plain",
                                    "data": base64.b64encode(expected_text.encode("utf-8")).decode("utf-8"),
                                },
                            },
                        ],
                    },
                },
            },
            expected_status=200,
        )
        self.expect(message.get("attachment_ids"), f"attachment flow did not persist attachment ids: {message}")
        attachment_id = message["attachment_ids"][0]
        attachments = self.get_json(f"/agent/attachments?thread_key={self.encode_path(key)}")
        self.expect(
            any(item.get("id") == attachment_id for item in attachments),
            f"attachment {attachment_id} missing from list: {attachments}",
        )
        downloaded = self.get_bytes(f"/agent/attachments/{attachment_id}/download").decode("utf-8")
        self.expect(downloaded == expected_text, f"downloaded attachment mismatch: {downloaded!r} != {expected_text!r}")
        execute = self.post_json(
            "/agent/execute",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "execute_id": f"exec-attachment-{uuid.uuid4().hex[:8]}",
                "harness": "amp",
                "delivery": {"platform": "qa"},
            },
            expected_status=202,
        )
        status = self.wait_for_terminal(execute["execution_id"])
        self.expect(status.get("status") == "completed", f"attachment execution did not complete: {status}")
        self.expect(status.get("result_text") == expected_text, f"attachment result mismatch: {status}")
        events = self.fetch_events(key, execute["execution_id"], after_event_id=0, max_time_s=1.5)
        self.ensure_final_delivery_ready(events, execute["execution_id"])
        delivered_events = self.mark_final_delivered(key, execute["execution_id"], self.latest_event_id(events))
        return {
            "thread_key": key,
            "execution_id": execute["execution_id"],
            "attachment_id": attachment_id,
            "downloaded": downloaded,
            "result_text": status["result_text"],
            "attachments_listed": [item.get("id") for item in attachments],
            "delivered_event_kinds": [event.get("event") for event in delivered_events],
        }

    def run_sandbox_api_dispatch(self) -> dict[str, Any]:
        parent_key = self.track_thread(self.thread_key("sandbox-dispatch"))
        child_key = self.track_thread(f"{parent_key}:child-legal")
        spawn = self.post_json("/agent/spawn", {"thread_key": parent_key, "harness": "amp"}, expected_status=200)
        child_prompt = (
            "Use the shell_command tool to run `printf \"%s\" \"$AGENT_PERSONA\"`. "
            "Then reply with exactly that value and nothing else."
        )
        sandbox_script = f"""python3 - <<'PY'\nimport json\nimport os\nimport urllib.error\nimport urllib.request\n\nBASE = os.environ.get('CENTAUR_API_URL', 'http://api:8000')\nwith open('/home/agent/.api_key', 'r', encoding='utf-8') as fh:\n    KEY = fh.read().strip()\nHEADERS = {{'Authorization': f'Bearer {{KEY}}'}}\nJSON_HEADERS = {{'Authorization': f'Bearer {{KEY}}', 'Content-Type': 'application/json'}}\nCHILD_KEY = {json.dumps(child_key)}\nCHILD_PROMPT = {json.dumps(child_prompt)}\n\ndef req(method, path, payload=None):\n    headers = dict(JSON_HEADERS if payload is not None else HEADERS)\n    data = json.dumps(payload).encode('utf-8') if payload is not None else None\n    request = urllib.request.Request(f'{{BASE}}{{path}}', data=data, headers=headers, method=method)\n    try:\n        with urllib.request.urlopen(request, timeout=60) as response:\n            raw = response.read()\n            body = json.loads(raw.decode('utf-8')) if raw else None\n            return response.status, body\n    except urllib.error.HTTPError as exc:\n        raw = exc.read()\n        body = json.loads(raw.decode('utf-8')) if raw else raw.decode('utf-8', errors='replace')\n        raise SystemExit(f'HTTP {{exc.code}} on {{path}}: {{body}}')\n\nspawn_status, spawn = req('POST', '/agent/spawn', {{\n    'thread_key': CHILD_KEY,\n    'harness': 'amp',\n    'persona_id': 'legal',\n}})\nif spawn_status != 200:\n    raise SystemExit(f'spawn failed: {{spawn_status}} {{spawn}}')\ngeneration = spawn['assignment_generation']\nmessage_status, message = req('POST', '/agent/message', {{\n    'thread_key': CHILD_KEY,\n    'assignment_generation': generation,\n    'role': 'user',\n    'parts': [{{'type': 'text', 'text': CHILD_PROMPT}}],\n}})\nif message_status != 200:\n    raise SystemExit(f'message failed: {{message_status}} {{message}}')\nexecute_status, execute = req('POST', '/agent/execute', {{\n    'thread_key': CHILD_KEY,\n    'assignment_generation': generation,\n    'execute_id': 'exec-child-' + CHILD_KEY.rsplit(':', 1)[-1],\n    'harness': 'amp',\n    'delivery': {{'platform': 'qa'}},\n}})\nif execute_status != 202:\n    raise SystemExit(f'execute failed: {{execute_status}} {{execute}}')\nprint(f'SANDBOX_API_DISPATCH_OK:{{execute["execution_id"]}}')\nPY"""
        prompt = (
            "Use the shell_command tool to run this exact script:\n\n"
            f"{sandbox_script}\n\n"
            "If it succeeds, reply with exactly the script's final line and nothing else."
        )
        self.post_json(
            "/agent/message",
            {
                "thread_key": parent_key,
                "assignment_generation": spawn["assignment_generation"],
                "role": "user",
                "parts": [{"type": "text", "text": prompt}],
            },
            expected_status=200,
        )
        execute = self.post_json(
            "/agent/execute",
            {
                "thread_key": parent_key,
                "assignment_generation": spawn["assignment_generation"],
                "execute_id": f"exec-sandbox-dispatch-{uuid.uuid4().hex[:8]}",
                "harness": "amp",
                "delivery": {"platform": "qa"},
            },
            expected_status=202,
        )
        status = self.wait_for_terminal(execute["execution_id"])
        self.expect(status.get("status") == "completed", f"sandbox dispatch did not complete: {status}")
        result_text = str(status.get("result_text") or "")
        self.expect(
            result_text.startswith("SANDBOX_API_DISPATCH_OK:"),
            f"sandbox dispatch result mismatch: {status}",
        )
        child_execution_id = result_text.split(":", 1)[1]
        parent_events = self.fetch_events(parent_key, execute["execution_id"], after_event_id=0, max_time_s=1.5)
        self.expect(self.has_tool_use(parent_events), "sandbox dispatch missing parent tool_use")
        self.expect(self.has_tool_result(parent_events), "sandbox dispatch missing parent tool_result")
        self.expect(
            self.raw_event_contains(parent_events, "SANDBOX_API_DISPATCH_OK"),
            "sandbox dispatch missing marker in parent stream",
        )
        self.ensure_final_delivery_ready(parent_events, execute["execution_id"])
        parent_delivered = self.mark_final_delivered(
            parent_key,
            execute["execution_id"],
            self.latest_event_id(parent_events),
        )
        child_status = self.wait_for_terminal(child_execution_id)
        self.expect(child_status.get("status") == "completed", f"child execution not completed: {child_status}")
        self.expect(child_status.get("result_text") == "legal", f"child execution result mismatch: {child_status}")
        child_events = self.fetch_events(child_key, child_execution_id, after_event_id=0, max_time_s=1.5)
        self.expect(child_events, "child execution produced no events")
        self.ensure_final_delivery_ready(child_events, child_execution_id)
        child_delivered = self.mark_final_delivered(
            child_key,
            child_execution_id,
            self.latest_event_id(child_events),
        )
        return {
            "thread_key": parent_key,
            "execution_id": execute["execution_id"],
            "result_text": result_text,
            "child_thread_key": child_key,
            "child_execution_id": child_execution_id,
            "child_result_text": child_status["result_text"],
            "parent_delivered_event_kinds": [event.get("event") for event in parent_delivered],
            "child_delivered_event_kinds": [event.get("event") for event in child_delivered],
        }

    def run_agent_tool_access_smoke(self) -> dict[str, Any]:
        key = self.track_thread(self.thread_key("agent-tool-access"))
        spawn = self.post_json("/agent/spawn", {"thread_key": key, "harness": "amp"}, expected_status=200)
        script = """python3 - <<'PY'\nimport subprocess\n\ndemo = subprocess.run(['call', 'demo', 'ping'], capture_output=True, text=True, check=True).stdout\nif 'pong' not in demo.lower():\n    raise SystemExit(f'unexpected demo output: {demo!r}')\nslack = subprocess.run(['call', 'slack', 'list_channels', '{\"limit\":1}'], capture_output=True, text=True, check=True).stdout\nif not slack.strip():\n    raise SystemExit('slack list_channels returned empty output')\nprint('AGENT_TOOL_SMOKE_OK')\nPY"""
        prompt = (
            "Use the shell_command tool to run this exact script:\n\n"
            f"{script}\n\n"
            "If it succeeds, reply with exactly AGENT_TOOL_ACCESS_OK and nothing else."
        )
        self.post_json(
            "/agent/message",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "role": "user",
                "parts": [{"type": "text", "text": prompt}],
            },
            expected_status=200,
        )
        execute = self.post_json(
            "/agent/execute",
            {
                "thread_key": key,
                "assignment_generation": spawn["assignment_generation"],
                "execute_id": f"exec-agent-tool-access-{uuid.uuid4().hex[:8]}",
                "harness": "amp",
                "delivery": {"platform": "qa"},
            },
            expected_status=202,
        )
        status = self.wait_for_terminal(execute["execution_id"])
        self.expect(status.get("status") == "completed", f"agent tool access did not complete: {status}")
        self.expect(status.get("result_text") == "AGENT_TOOL_ACCESS_OK", f"agent tool access result mismatch: {status}")
        events = self.fetch_events(key, execute["execution_id"], after_event_id=0, max_time_s=1.5)
        self.expect(self.has_tool_use(events), "agent tool access missing tool_use")
        self.expect(self.has_tool_result(events), "agent tool access missing tool_result")
        self.expect(
            self.raw_event_contains(events, "AGENT_TOOL_SMOKE_OK"),
            "agent tool access missing shell-command marker",
        )
        self.ensure_final_delivery_ready(events, execute["execution_id"])
        delivered = self.mark_final_delivered(key, execute["execution_id"], self.latest_event_id(events))
        return {
            "thread_key": key,
            "execution_id": execute["execution_id"],
            "status": status["status"],
            "result_text": status["result_text"],
            "delivered_event_kinds": [event.get("event") for event in delivered],
        }

    def cleanup_threads_safely(self) -> None:
        for key in reversed(self.created_threads):
            try:
                self.release_thread(key)
            except Exception as exc:  # noqa: BLE001
                self.cleanup_errors.append(f"{key}: {exc}")

    def run(self) -> dict[str, Any]:
        self.summary["external_health"] = self.wait_external_health()
        self.summary["external_api_key"] = self.mint_external_api_key()
        self.summary["ready"] = self.wait_authenticated_agent_api()
        try:
            self.summary["scenarios"]["negative"] = self.run_negative_checks()
            self.summary["scenarios"]["normal_prompt"] = self.run_normal_prompt()
            self.summary["scenarios"]["reconnect"] = self.run_reconnect_flow()
            self.summary["scenarios"]["cancel_reconnect"] = self.run_cancel_reconnect_flow()
            self.summary["scenarios"]["persona_legal"] = self.run_persona("legal")
            self.summary["scenarios"]["persona_events"] = self.run_persona("events")
            self.summary["scenarios"]["attachments"] = self.run_attachment_flow()
            self.summary["scenarios"]["sandbox_api_dispatch"] = self.run_sandbox_api_dispatch()
            if self.with_agent_tool_smoke:
                self.summary["scenarios"]["agent_tool_access"] = self.run_agent_tool_access_smoke()
            self.summary["ok"] = True
            return self.summary
        finally:
            self.cleanup_threads_safely()
            if self.cleanup_errors:
                self.summary["cleanup_errors"] = self.cleanup_errors
            self.revoke_external_api_key()
            self.summary["finished_at"] = int(time.time())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run durable agent runtime QA through the configured API path, "
            "with optional in-agent API tool access smoke checks."
        ),
    )
    parser.add_argument("--api-url", default=DEFAULT_EXTERNAL_URL)
    parser.add_argument("--api-container", default=DEFAULT_API_CONTAINER)
    parser.add_argument("--ready-timeout-s", type=float, default=DEFAULT_READY_TIMEOUT_S)
    parser.add_argument("--execution-timeout-s", type=float, default=DEFAULT_EXECUTION_TIMEOUT_S)
    parser.add_argument("--event-timeout-s", type=float, default=DEFAULT_EVENT_TIMEOUT_S)
    parser.add_argument("--event-poll-ms", type=int, default=DEFAULT_EVENT_POLL_MS)
    parser.add_argument("--poll-interval-s", type=float, default=DEFAULT_POLL_INTERVAL_S)
    parser.add_argument("--request-timeout-s", type=float, default=DEFAULT_REQUEST_TIMEOUT_S)
    parser.add_argument(
        "--with-agent-tool-smoke",
        action="store_true",
        help="Also prove a spawned agent can reach API tools through the call helper.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = QARunner(args)
    try:
        result = runner.run()
    except Exception as exc:  # noqa: BLE001
        runner.summary["ok"] = False
        runner.summary["error"] = str(exc)
        runner.summary["traceback"] = traceback.format_exc()
        try:
            runner.cleanup_threads_safely()
        except Exception:
            pass
        try:
            runner.revoke_external_api_key()
        except Exception:
            pass
        runner.summary["finished_at"] = int(time.time())
        print(json.dumps(runner.summary, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
