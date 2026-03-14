"""Sandbox backend interface — pluggable agent execution environments."""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimeState:
    """Process-local ephemeral state for a running sandbox.

    Keyed by sandbox_id in agent._runtime. Contains the aiodocker Stream
    handle and turn bookkeeping. Fully async — no threads or queues.
    """

    turn_counter: int = 0
    stream: Any = None  # aiodocker Stream (read_out / write_in)
    busy: bool = False
    last_result: str | None = None


@dataclass
class SandboxSession:
    """Represents a running sandbox (container or VM)."""

    sandbox_id: str  # backend-specific ID (container ID, VM ID, etc.)
    thread_key: str
    harness: str
    engine: str
    started_at: float = 0.0
    backend_name: str = ""  # "docker", "iron", etc.
    db_state: str = ""


class SandboxBackend(abc.ABC):
    """ABC for agent sandbox orchestration backends."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short identifier for this backend (e.g. 'docker', 'iron')."""

    @property
    def supports_warm_pool(self) -> bool:
        """Whether this backend supports pre-warming sandboxes."""
        return False

    @abc.abstractmethod
    async def create(
        self,
        thread_key: str,
        harness: str,
        engine: str,
        *,
        persona: str | None = None,
        repo: str | None = None,
        warm: bool = False,
        model: str | None = None,
    ) -> SandboxSession:
        """Create and start a new sandbox. Block until ready."""

    @abc.abstractmethod
    async def attach(self, session: SandboxSession, *, logs: bool = False) -> None:
        """Attach stdin/stdout streams to the sandbox.

        If logs=True, include buffered output from before the attach point.
        """

    @abc.abstractmethod
    async def write_stdin(self, session: SandboxSession, obj: dict) -> None:
        """Write an NDJSON line to the sandbox's stdin."""

    @abc.abstractmethod
    async def stream_stdout(self, session: SandboxSession) -> AsyncIterator[str]:
        """Yield stdout lines from the sandbox asynchronously."""
        yield  # pragma: no cover — abstract async generator

    @abc.abstractmethod
    async def stop(self, session: SandboxSession) -> None:
        """Stop and clean up the sandbox."""

    @abc.abstractmethod
    async def status(self, session: SandboxSession) -> str:
        """Return sandbox status: 'running', 'stopped', 'gone', etc."""

    async def close_streams(self, session: SandboxSession) -> None:
        """Close any open streams. Default: no-op."""

    async def exec_run(
        self, sandbox_id: str, cmd: list[str], *, environment: dict | None = None, user: str = ""
    ) -> tuple[int, bytes]:
        """Run a command inside a container and return (exit_code, output)."""
        raise NotImplementedError

    async def status_by_id(self, sandbox_id: str) -> str:
        """Check container status by ID (no session needed)."""
        raise NotImplementedError

    async def stop_by_id(self, sandbox_id: str) -> None:
        """Stop and remove a container by ID (no session needed)."""
        raise NotImplementedError

    async def rename_by_id(self, sandbox_id: str, new_name: str) -> None:
        """Rename a container by ID."""
        raise NotImplementedError

    async def refresh_token_by_id(self, sandbox_id: str, new_token: str) -> None:
        """Write a fresh API token into a running container."""
        raise NotImplementedError

    async def recover_warm(self, pool_harness: str) -> list[SandboxSession]:
        """Discover warm (pre-created, unclaimed) sandboxes. Default: empty."""
        return []
