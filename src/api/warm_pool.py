"""Pre-warmed sandbox pool — keeps N sandboxes ready for instant claiming.

Eliminates sandbox startup latency (~15s) by maintaining a pool of idle
sandboxes that can be instantly claimed when a new thread arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from dataclasses import dataclass, field

import structlog

from api.deps import mint_sandbox_token
from api.sandbox.base import SandboxSession
from api.sandbox.registry import get_backend

log = structlog.get_logger()

# Pool configuration
POOL_SIZE = int(os.getenv("WARM_POOL_SIZE", "5"))
POOL_HARNESS = os.getenv("WARM_POOL_HARNESS", "amp")
POOL_REPLENISH_INTERVAL = float(os.getenv("WARM_POOL_REPLENISH_INTERVAL", "5.0"))


@dataclass
class WarmContainer:
    """A pre-warmed sandbox not yet bound to any thread."""

    sandbox_id: str
    harness: str
    engine: str
    created_at: float = field(default_factory=time.time)


# Thread-safe pool (accessed from async + sync contexts)
_pool_lock = threading.Lock()
_pool: list[WarmContainer] = []
_replenish_task: asyncio.Task | None = None


def pool_size() -> int:
    """Current number of warm sandboxes ready."""
    with _pool_lock:
        return len(_pool)


def pool_status() -> dict:
    """Return pool diagnostics."""
    with _pool_lock:
        containers = [
            {"sandbox_id": w.sandbox_id[:12], "age_s": round(time.time() - w.created_at, 1)}
            for w in _pool
        ]
    return {
        "target_size": POOL_SIZE,
        "current_size": len(containers),
        "harness": POOL_HARNESS,
        "containers": containers,
    }


def _spawn_warm_container() -> WarmContainer | None:
    """Synchronously create one warm sandbox. Returns None on failure."""
    backend = get_backend()
    if not backend.supports_warm_pool:
        return None

    engines = {"amp": "amp", "claude-code": "claude-code", "codex": "codex"}
    engine = engines.get(POOL_HARNESS, "amp")

    placeholder_key = f"warm-{int(time.time() * 1000)}-{id(threading.current_thread())}"
    try:
        session = backend.create(placeholder_key, POOL_HARNESS, engine, warm=True)
        warm = WarmContainer(
            sandbox_id=session.sandbox_id,
            harness=POOL_HARNESS,
            engine=engine,
        )
        log.info("warm_container_created", sandbox=session.sandbox_id[:12])
        return warm
    except Exception as exc:
        log.warning("warm_container_spawn_failed", error=str(exc))
        return None


def _replenish_sync() -> int:
    """Spawn sandboxes until the pool reaches target size. Returns count spawned."""
    backend = get_backend()
    if not backend.supports_warm_pool:
        return 0

    spawned = 0
    while True:
        with _pool_lock:
            deficit = POOL_SIZE - len(_pool)
        if deficit <= 0:
            break
        warm = _spawn_warm_container()
        if warm is None:
            break
        with _pool_lock:
            _pool.append(warm)
        spawned += 1
    return spawned


def claim_container(thread_key: str, harness: str = "amp") -> SandboxSession | None:
    """Try to claim a warm sandbox from the pool. Returns SandboxSession or None.

    Only returns a sandbox if the requested harness matches the pool harness.
    """
    if harness != POOL_HARNESS:
        return None

    warm: WarmContainer | None = None
    with _pool_lock:
        if _pool:
            warm = _pool.pop(0)

    if warm is None:
        return None

    backend = get_backend()

    # Verify sandbox is still running
    dummy_session = SandboxSession(
        sandbox_id=warm.sandbox_id,
        thread_key="",
        harness=warm.harness,
        engine=warm.engine,
        backend_name=backend.name,
    )
    st = backend.status(dummy_session)
    if st != "running":
        log.warning("warm_container_dead_on_claim", sandbox=warm.sandbox_id[:12])
        with contextlib.suppress(Exception):
            backend.stop(dummy_session)
        return None

    # Rename sandbox to match the thread key
    new_name = f"pipe-{thread_key.replace(':', '-').replace('.', '-')[:40]}"
    backend.rename(dummy_session, new_name)

    # Mint a fresh sandbox token and inject it into the container.
    # The original token was created at pool-spawn time and may have expired.
    try:
        fresh_token = mint_sandbox_token(thread_key, new_name)
        backend.refresh_token(dummy_session, fresh_token)
    except Exception:
        log.warning("warm_claim_token_refresh_failed", sandbox=warm.sandbox_id[:12])

    session = SandboxSession(
        sandbox_id=warm.sandbox_id,
        thread_key=thread_key,
        harness=harness,
        engine=warm.engine,
        started_at=time.time(),
        backend_name=backend.name,
    )
    # Initialize orchestration metadata
    session.metadata["turn_counter"] = 0
    session.metadata["_active_turn_id"] = 0
    session.metadata["_turn_lock"] = threading.Lock()
    session.metadata["_active_queue"] = None

    log.info(
        "warm_container_claimed",
        thread_key=thread_key,
        sandbox=warm.sandbox_id[:12],
        pool_age_s=round(time.time() - warm.created_at, 1),
    )
    return session


def _cleanup_pool_sync() -> int:
    """Stop and remove all warm sandboxes. Returns count cleaned."""
    with _pool_lock:
        to_clean = list(_pool)
        _pool.clear()
    cleaned = 0
    backend = get_backend()
    for warm in to_clean:
        with contextlib.suppress(Exception):
            dummy = SandboxSession(
                sandbox_id=warm.sandbox_id,
                thread_key="",
                harness=warm.harness,
                engine=warm.engine,
                backend_name=backend.name,
            )
            backend.stop(dummy)
            cleaned += 1
    return cleaned


# ── Async API ────────────────────────────────────────────────────────────────


async def replenish() -> int:
    """Async wrapper — spawn missing warm sandboxes."""
    return await asyncio.to_thread(_replenish_sync)


async def cleanup_pool() -> int:
    """Async wrapper — tear down all warm sandboxes."""
    return await asyncio.to_thread(_cleanup_pool_sync)


def _recover_warm_sync() -> int:
    """Recover existing warm sandboxes from backend on API restart."""
    backend = get_backend()
    if not backend.supports_warm_pool:
        return 0

    recovered = 0
    sessions = backend.recover_warm(POOL_HARNESS)
    with _pool_lock:
        for session in sessions:
            if len(_pool) >= POOL_SIZE:
                break
            _pool.append(
                WarmContainer(
                    sandbox_id=session.sandbox_id,
                    harness=session.harness,
                    engine=session.engine,
                )
            )
            recovered += 1
    return recovered


async def start_replenish_loop() -> asyncio.Task:
    """Start a background task that keeps the pool at target size."""
    global _replenish_task

    async def _loop() -> None:
        # Recover any surviving warm sandboxes from a previous run
        recovered = await asyncio.to_thread(_recover_warm_sync)
        if recovered:
            log.info("warm_pool_recovered", recovered=recovered)
        # Fill the rest
        count = await replenish()
        if count:
            log.info("warm_pool_initial_fill", spawned=count, target=POOL_SIZE)
        while True:
            await asyncio.sleep(POOL_REPLENISH_INTERVAL)
            try:
                await replenish()
            except Exception as exc:
                log.warning("warm_pool_replenish_error", error=str(exc))

    _replenish_task = asyncio.create_task(_loop())
    return _replenish_task


async def stop_replenish_loop() -> None:
    """Cancel replenish loop and drain the pool."""
    global _replenish_task
    if _replenish_task:
        _replenish_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _replenish_task
        _replenish_task = None
    cleaned = await cleanup_pool()
    if cleaned:
        log.info("warm_pool_drained", cleaned=cleaned)
