"""Stateless pipe agent — spawn containers, pipe stdin/stdout, no Postgres.

Thin wrapper around Docker: one container per thread_key, raw NDJSON streaming.
Sessions are in-memory only and reconstructable from Docker labels on restart.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import os
import struct
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import docker
import structlog
from docker.errors import NotFound

from shared.tool_sdk import _sm_read

log = structlog.get_logger()

# Docker stream multiplexing header: 8 bytes
# [stream_type(1), 0, 0, 0, size(4 big-endian)]
_HEADER_SIZE = 8
_STDOUT = 1
_STDERR = 2


@dataclass
class PipeSession:
    container_id: str
    thread_key: str
    harness: str
    engine: str
    sock: Any = field(default=None, repr=False)
    exec_id: str | None = None
    started_at: float = 0.0


# In-memory sessions — reconstructable from Docker labels on restart
_sessions: dict[str, PipeSession] = {}


def _docker_client() -> docker.DockerClient:
    return docker.from_env()


def _image() -> str:
    return os.getenv("AGENT_IMAGE", "agent2:latest")


def _repos_host_dir() -> str:
    return os.getenv("REPOS_HOST_DIR", os.path.expanduser("~/github"))


def _fetch_secret(key: str) -> str:
    return _sm_read(key) or os.getenv(key, "")


def _sm_list_keys() -> list[str]:
    url = os.environ.get("SECRET_MANAGER_URL", "http://secrets:8100")
    try:
        import httpx

        resp = httpx.get(f"{url}/keys", timeout=5.0)
        if resp.status_code == 200:
            return resp.json().get("keys", [])
    except Exception:
        pass
    return []


def _container_env() -> list[str]:
    """Build env vars for sandbox containers."""
    local_dev = os.getenv("AGENT_LOCAL_DEV", "").lower() in ("1", "true")
    env = [
        f"AI_V2_API_URL={os.getenv('AGENT_API_URL', 'http://api:8000')}",
        f"AI_V2_API_KEY={_fetch_secret('API_SECRET_KEY')}",
    ]

    if local_dev:
        for key in _sm_list_keys():
            real = _fetch_secret(key).strip()
            if real:
                env.append(f"{key}={real}")
    else:
        firewall_host = os.getenv("FIREWALL_HOST", "firewall")
        for key in _sm_list_keys():
            env.append(f"{key}={key}")
        env.extend(
            [
                f"HTTPS_PROXY=http://{firewall_host}:8080",
                f"HTTP_PROXY=http://{firewall_host}:8080",
                f"https_proxy=http://{firewall_host}:8080",
                f"http_proxy=http://{firewall_host}:8080",
                "NO_PROXY=api,localhost,127.0.0.1",
                "no_proxy=api,localhost,127.0.0.1",
                "NODE_EXTRA_CA_CERTS=/firewall-certs/ca-cert.pem",
                "REQUESTS_CA_BUNDLE=/firewall-certs/ca-cert.pem",
                "SSL_CERT_FILE=/firewall-certs/ca-cert.pem",
                "GIT_SSL_CAINFO=/firewall-certs/ca-cert.pem",
            ]
        )

    return env


def _container_recent_logs(container: Any, tail: int = 40, max_chars: int = 2000) -> str:
    try:
        raw = container.logs(tail=tail)
    except Exception:
        return ""
    text = (
        raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    )
    text = text.strip()
    return text[-max_chars:] if len(text) > max_chars else text


def _wait_ready(container: Any, timeout: int = 15) -> float:
    """Wait for the entrypoint to signal readiness (touch ~/.ready)."""
    t0 = time.monotonic()
    deadline = t0 + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            container.reload()
        status = str(getattr(container, "status", "") or "")
        if status and status not in {"created", "running"}:
            logs = _container_recent_logs(container)
            detail = f"sandbox exited before ready (status={status})"
            if logs:
                detail += f"; last logs: {logs}"
            raise RuntimeError(detail)
        try:
            exit_code, _ = container.exec_run(["test", "-f", "/home/agent/.ready"], demux=False)
        except Exception:
            time.sleep(0.1)
            continue
        if exit_code == 0:
            return round(time.monotonic() - t0, 3)
        time.sleep(0.1)
    raise TimeoutError(f"sandbox readiness timed out after {timeout}s")


def _build_command(engine: str, message: str) -> list[str]:
    """Build the harness CLI command (non-persistent, single-turn)."""
    if engine == "claude-code":
        return [
            "claude",
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "-p",
            message,
        ]
    if engine == "codex":
        codex_model = os.getenv("AGENT_CODEX_MODEL", "gpt-5.3-codex").strip() or "gpt-5.3-codex"
        return [
            "codex",
            "exec",
            "--model",
            codex_model,
            "--json",
            "--full-auto",
            "--skip-git-repo-check",
            message,
        ]
    # Default: amp
    return [
        "amp",
        "--no-ide",
        "--no-notifications",
        "--dangerously-allow-all",
        "--stream-json",
        "-x",
        message,
    ]


def _create_container(
    client: docker.DockerClient,
    thread_key: str,
    harness: str,
    engine: str,
) -> Any:
    """Create and start a labeled agent container. Returns the container object."""
    repos_dir = os.path.abspath(_repos_host_dir())
    env = _container_env()

    container_name = f"pipe-{thread_key.replace(':', '-').replace('.', '-')[:40]}"

    # Remove stale container with same name
    with contextlib.suppress(Exception):
        stale = client.containers.get(container_name)
        stale.remove(force=True)

    labels = {
        "agent2": "true",
        "ai2.pipe": "true",
        "ai2.thread": thread_key,
        "ai2.harness": harness,
        "ai2.engine": engine,
    }
    volumes: dict[str, dict[str, str]] = {
        repos_dir: {"bind": "/home/agent/github", "mode": "ro"},
    }
    vol = os.getenv("FIREWALL_CERTS_VOLUME", "firewall-certs")
    volumes[vol] = {"bind": "/firewall-certs", "mode": "ro"}

    container = client.containers.run(
        _image(),
        detach=True,
        stdin_open=True,
        tty=False,
        network_mode=os.getenv("AGENT_NETWORK", "ai_v2_default"),
        mem_limit="4g",
        nano_cpus=int(2 * 1e9),
        environment=env,
        working_dir="/home/agent",
        volumes=volumes,
        labels=labels,
        name=container_name,
    )
    _wait_ready(container)
    return container


def _spawn_sync(thread_key: str, harness: str) -> PipeSession:
    """Synchronous spawn: create container + register session."""
    engines = {"amp": "amp", "claude-code": "claude-code", "codex": "codex"}
    engine = engines.get(harness, "amp")

    client = _docker_client()
    container = _create_container(client, thread_key, harness, engine)

    session = PipeSession(
        container_id=container.id,
        thread_key=thread_key,
        harness=harness,
        engine=engine,
        started_at=time.time(),
    )
    _sessions[thread_key] = session
    log.info("pipe_session_spawned", thread_key=thread_key, container=container.id[:12])
    return session


def _exec_and_stream_sync(session: PipeSession, message: str):
    """Start docker exec and yield raw stdout lines (blocking generator)."""
    client = _docker_client()
    api = client.api
    cmd = _build_command(session.engine, message)

    exec_id = api.exec_create(
        session.container_id,
        cmd,
        stdin=True,
        stdout=True,
        stderr=True,
        workdir="/home/agent/workspace",
    )["Id"]
    session.exec_id = exec_id

    # Get a raw multiplexed socket
    sock = api.exec_start(exec_id, socket=True)
    raw = sock._sock

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    stdout_buf = ""

    def _recvall(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = raw.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed")
            buf += chunk
        return buf

    try:
        while True:
            try:
                header = _recvall(_HEADER_SIZE)
            except ConnectionError:
                break
            stream_type = header[0]
            payload_len = struct.unpack(">I", header[4:8])[0]
            if payload_len == 0:
                continue
            payload = _recvall(payload_len)

            if stream_type == _STDOUT:
                stdout_buf += decoder.decode(payload)
                while "\n" in stdout_buf:
                    line, stdout_buf = stdout_buf.split("\n", 1)
                    stripped = line.strip()
                    if stripped:
                        yield stripped
            elif stream_type == _STDERR:
                text = payload.decode("utf-8", errors="replace").strip()
                if text:
                    yield json.dumps({"type": "stderr", "text": text})

        # Flush remaining
        final = decoder.decode(b"", final=True)
        stdout_buf += final
        remaining = stdout_buf.strip()
        if remaining:
            yield remaining
    finally:
        with contextlib.suppress(Exception):
            sock.close()


def _stop_sync(thread_key: str) -> bool:
    """Stop container and remove session. Returns True if stopped."""
    session = _sessions.pop(thread_key, None)
    if not session:
        return False
    client = _docker_client()
    with contextlib.suppress(Exception):
        container = client.containers.get(session.container_id)
        container.stop(timeout=5)
        container.remove()
    log.info("pipe_session_stopped", thread_key=thread_key)
    return True


def _get_status_sync(thread_key: str) -> dict[str, Any]:
    """Check if a session/container is alive."""
    session = _sessions.get(thread_key)
    if not session:
        return {"thread_key": thread_key, "status": "not_found"}
    client = _docker_client()
    try:
        container = client.containers.get(session.container_id)
        return {
            "thread_key": thread_key,
            "status": container.status,
            "container_id": session.container_id[:12],
            "harness": session.harness,
            "engine": session.engine,
            "started_at": session.started_at,
        }
    except NotFound:
        _sessions.pop(thread_key, None)
        return {"thread_key": thread_key, "status": "gone"}


def _recover_sync() -> dict[str, Any]:
    """Scan Docker for containers with ai2.pipe label, rebuild _sessions."""
    client = _docker_client()
    recovered = 0
    try:
        containers = client.containers.list(filters={"label": "ai2.pipe=true"})
    except Exception as exc:
        log.warning("pipe_recover_failed", error=str(exc))
        return {"recovered": 0, "error": str(exc)}

    for container in containers:
        thread_key = container.labels.get("ai2.thread", "")
        if not thread_key or thread_key in _sessions:
            continue
        if container.status != "running":
            continue
        _sessions[thread_key] = PipeSession(
            container_id=container.id,
            thread_key=thread_key,
            harness=container.labels.get("ai2.harness", "amp"),
            engine=container.labels.get("ai2.engine", "amp"),
            started_at=time.time(),
        )
        recovered += 1
        log.info("pipe_session_recovered", thread_key=thread_key)

    return {"recovered": recovered}


# ── Async public API (wraps sync Docker calls in threads) ────────────────


async def get_or_spawn(thread_key: str, harness: str = "amp") -> PipeSession:
    """Get existing session or spawn a new container."""
    session = _sessions.get(thread_key)
    if session:
        # Verify container is still alive
        client = await asyncio.to_thread(_docker_client)
        try:
            container = await asyncio.to_thread(client.containers.get, session.container_id)
            if container.status == "running":
                return session
        except NotFound:
            pass
        _sessions.pop(thread_key, None)

    return await asyncio.to_thread(_spawn_sync, thread_key, harness)


async def stream_exec(session: PipeSession, message: str) -> AsyncIterator[str]:
    """Run a command in the container and yield raw stdout lines."""
    loop = asyncio.get_event_loop()
    q: asyncio.Queue[str | None] = asyncio.Queue()

    def _run() -> None:
        try:
            for line in _exec_and_stream_sync(session, message):
                loop.call_soon_threadsafe(q.put_nowait, line)
        except Exception as exc:
            loop.call_soon_threadsafe(
                q.put_nowait,
                json.dumps({"type": "error", "message": str(exc)}),
            )
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    thread = asyncio.to_thread(_run)
    task = asyncio.ensure_future(thread)

    try:
        while True:
            item = await q.get()
            if item is None:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def stop_session(thread_key: str) -> bool:
    return await asyncio.to_thread(_stop_sync, thread_key)


async def get_status(thread_key: str) -> dict[str, Any]:
    return await asyncio.to_thread(_get_status_sync, thread_key)


async def recover_sessions() -> dict[str, Any]:
    return await asyncio.to_thread(_recover_sync)
