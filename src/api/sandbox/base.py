"""Sandbox backend interface — pluggable agent execution environments."""

from __future__ import annotations

import abc
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SandboxSession:
    """Represents a running sandbox (container or VM)."""

    sandbox_id: str  # backend-specific ID (container ID, VM ID, etc.)
    thread_key: str
    harness: str
    engine: str
    started_at: float = 0.0
    backend_name: str = ""  # "docker", "iron", etc.
    metadata: dict[str, Any] = field(default_factory=dict)


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
    def create(
        self,
        thread_key: str,
        harness: str,
        engine: str,
        *,
        warm: bool = False,
    ) -> SandboxSession:
        """Create and start a new sandbox. Block until ready."""

    @abc.abstractmethod
    def attach(self, session: SandboxSession, *, logs: bool = False) -> None:
        """Attach stdin/stdout streams to the sandbox.

        If logs=True, include buffered output from before the attach point.
        """

    @abc.abstractmethod
    def write_stdin(self, session: SandboxSession, obj: dict) -> None:
        """Write an NDJSON line to the sandbox's stdin."""

    @abc.abstractmethod
    def stream_stdout(self, session: SandboxSession) -> Iterator[str]:
        """Yield stdout lines from the sandbox. Blocks until EOF."""

    @abc.abstractmethod
    def stop(self, session: SandboxSession) -> None:
        """Stop and clean up the sandbox."""

    @abc.abstractmethod
    def status(self, session: SandboxSession) -> str:
        """Return sandbox status: 'running', 'stopped', 'gone', etc."""

    @abc.abstractmethod
    def recover(self) -> list[SandboxSession]:
        """Discover running sandboxes from this backend (for crash recovery)."""

    def close_streams(self, session: SandboxSession) -> None:  # noqa: B027
        """Close any open streams. Default: no-op."""

    def recent_logs(self, session: SandboxSession, tail: int = 40) -> str:
        """Return recent log output. Default: empty."""
        return ""

    def rename(self, session: SandboxSession, new_name: str) -> None:  # noqa: B027
        """Rename a sandbox (e.g. when claiming a warm container). Default: no-op."""

    def refresh_token(self, session: SandboxSession, new_token: str) -> None:  # noqa: B027
        """Inject a fresh API token into a running sandbox. Default: no-op."""

    def recover_warm(self, pool_harness: str) -> list[SandboxSession]:
        """Discover warm (pre-created, unclaimed) sandboxes. Default: empty."""
        return []
