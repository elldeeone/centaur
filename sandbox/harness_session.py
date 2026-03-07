#!/usr/bin/env python3
"""Lightweight adapter (PID 1) that wraps a harness CLI inside the sandbox container.

Communicates with the API over NDJSON on stdin/stdout. Starts the harness once
(amp/claude-code) or per-turn (codex/pi-mono) and forwards ALL output transparently.
"""

import json
import os
import signal
import subprocess
import sys
import threading

WORKSPACE = "/home/agent/workspace"

engine = os.environ.get("AGENT_ENGINE", "amp")
model_override = os.environ.get("AGENT_MODEL", "")
persona = os.environ.get("AGENT_PERSONA", "")

write_lock = threading.Lock()
proc: subprocess.Popen | None = None
agent_thread_id: str | None = None
current_turn_id: int | None = None
last_result_text: str | None = None


def emit(obj: dict) -> None:
    with write_lock:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()


# ── Harness command builders ────────────────────────────────────────────────

def _system_prompt_path() -> str | None:
    if not persona:
        return None
    p = os.path.expanduser(f"~/AGENTS_{persona.upper()}.md")
    return p if os.path.isfile(p) else None


def build_persistent_cmd() -> list[str]:
    if engine == "amp":
        cmd = [
            "amp", "--no-ide", "--no-notifications", "--dangerously-allow-all",
            "--execute", "--stream-json", "--stream-json-input",
        ]
        if model_override:
            cmd.extend(["--model", model_override])
        return cmd
    cmd = [
        "claude", "--dangerously-skip-permissions",
        "--output-format", "stream-json", "--input-format", "stream-json",
        "--verbose",
    ]
    if model_override:
        cmd.extend(["--model", model_override])
    sp = _system_prompt_path()
    if sp:
        cmd.extend(["--system-prompt", sp])
    return cmd


def build_oneshot_cmd(message: str, thread_id: str | None) -> list[str]:
    if engine == "codex":
        m = model_override or os.environ.get("AGENT_CODEX_MODEL", "gpt-5.3-codex")
        cmd = ["codex", "exec", "--model", m, "--json", "--full-auto", "--skip-git-repo-check"]
        if thread_id:
            cmd.extend(["resume", thread_id])
        cmd.append(message)
        return cmd
    cmd = ["pi", "--mode", "json"]
    if thread_id:
        cmd.extend(["--session", thread_id])
    cmd.append(message)
    return cmd


# ── Event inspection ───────────────────────────────────────────────────────

def _extract_thread_id(event: dict) -> None:
    global agent_thread_id
    t = event.get("type", "")
    if engine in ("amp", "claude-code"):
        if t == "system" and event.get("subtype") == "init":
            agent_thread_id = event.get("session_id") or agent_thread_id
    elif engine == "codex":
        if t == "thread.started":
            agent_thread_id = event.get("thread_id") or agent_thread_id
    elif engine == "pi-mono" and t == "session":
        agent_thread_id = event.get("id") or agent_thread_id


def _is_turn_done(event: dict) -> bool:
    t = event.get("type", "")
    if engine in ("amp", "claude-code"):
        return t == "result"
    if engine == "codex":
        return t in ("turn.completed", "turn.failed")
    return t == "agent_end"  # pi-mono


def _extract_result(event: dict) -> None:
    global last_result_text
    t = event.get("type", "")
    if engine in ("amp", "claude-code") and t == "result":
        last_result_text = event.get("result", "")
    elif engine == "codex" and t == "item.completed":
        item = event.get("item", {})
        if item.get("type") == "agent_message":
            last_result_text = item.get("text", "")
    elif engine == "pi-mono" and t == "message_end":
        msg = event.get("message", {})
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if content:
                last_result_text = content[-1].get("text", "")


# ── Output forwarding ─────────────────────────────────────────────────────

def forward_stdout(p: subprocess.Popen) -> None:
    global last_result_text, current_turn_id
    assert p.stdout is not None
    for raw in p.stdout:
        line = raw.rstrip("\n")
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            emit({"type": "raw", "text": line})
            continue
        emit(event)
        _extract_thread_id(event)
        _extract_result(event)
        if _is_turn_done(event):
            emit({
                "type": "turn.done", "turn_id": current_turn_id,
                "result": last_result_text or "",
                "agent_thread_id": agent_thread_id or "",
            })
            current_turn_id = None
            last_result_text = None
    # Harness stdout closed — if mid-turn, emit turn.done with exit code
    if current_turn_id is not None:
        code = p.poll()
        emit({"type": "error", "error": f"Harness exited unexpectedly (code {code})"})
        emit({
            "type": "turn.done", "turn_id": current_turn_id,
            "result": last_result_text or "",
            "agent_thread_id": agent_thread_id or "",
            "exit_code": code,
        })
        current_turn_id = None
        last_result_text = None


def forward_stderr(p: subprocess.Popen) -> None:
    assert p.stderr is not None
    for raw in p.stderr:
        line = raw.rstrip("\n")
        if line:
            emit({"type": "stderr", "text": line})


def start_harness(cmd: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, cwd=WORKSPACE, bufsize=1,
    )


def _spawn_forwarders(p: subprocess.Popen) -> tuple[threading.Thread, threading.Thread]:
    out = threading.Thread(target=forward_stdout, args=(p,), daemon=True)
    err = threading.Thread(target=forward_stderr, args=(p,), daemon=True)
    out.start()
    err.start()
    return out, err


# ── Main loops ─────────────────────────────────────────────────────────────

def read_commands():
    """Yield parsed NDJSON commands from stdin, handling ping inline."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "ping":
            emit({"type": "pong"})
            continue
        yield msg


def run_persistent() -> None:
    """amp / claude-code: start once, pipe turns via stdin."""
    global proc, current_turn_id
    for msg in read_commands():
        mtype = msg.get("type", "")
        if mtype == "interrupt":
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        elif mtype == "turn.start":
            current_turn_id = msg.get("turn_id")
            text = msg.get("text", "")
            if proc is None or proc.poll() is not None:
                proc = start_harness(build_persistent_cmd())
                _spawn_forwarders(proc)
            assert proc is not None and proc.stdin is not None
            proc.stdin.write(
                json.dumps({"type": "user", "text": text}) + "\n"
            )
            proc.stdin.flush()


def run_oneshot() -> None:
    """codex / pi-mono: spawn a new process per turn."""
    global proc, current_turn_id, last_result_text
    for msg in read_commands():
        mtype = msg.get("type", "")
        if mtype == "interrupt":
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        elif mtype == "turn.start":
            current_turn_id = msg.get("turn_id")
            last_result_text = None
            text = msg.get("text", "")
            proc = start_harness(build_oneshot_cmd(text, agent_thread_id))
            out_t, err_t = _spawn_forwarders(proc)
            proc.wait()
            out_t.join(timeout=2)
            err_t.join(timeout=2)
            if proc.returncode != 0 and current_turn_id is not None:
                emit({"type": "error",
                      "error": f"Harness exited with code {proc.returncode}"})
                emit({
                    "type": "turn.done", "turn_id": current_turn_id,
                    "result": last_result_text or "",
                    "agent_thread_id": agent_thread_id or "",
                })
                current_turn_id = None


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    emit({"type": "system", "subtype": "ready"})
    if engine in ("amp", "claude-code"):
        run_persistent()
    else:
        run_oneshot()


if __name__ == "__main__":
    main()
