"""Agent sandbox REST API — spawn, execute, stop, status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.agent import get_agent
from api.deps import verify_api_key

router = APIRouter(
    prefix="/agent",
    tags=["agent"],
    dependencies=[Depends(verify_api_key)],
)


class SpawnRequest(BaseModel):
    slack_thread_key: str
    harness: str = "amp"
    repo: str | None = None
    request_id: str | None = None


class ExecuteRequest(BaseModel):
    slack_thread_key: str
    message: str
    harness: str = "amp"
    repo: str | None = None
    request_id: str | None = None


class StopRequest(BaseModel):
    slack_thread_key: str


class InterruptRequest(BaseModel):
    slack_thread_key: str


@router.post("/spawn")
async def spawn(req: SpawnRequest) -> dict[str, Any]:
    """Spawn a sandbox container for a Slack thread."""
    agent = get_agent()
    return agent.spawn(req.slack_thread_key, req.harness, req.repo, req.request_id)


@router.post("/execute")
async def execute(req: ExecuteRequest) -> dict[str, Any]:
    """Execute a message in a sandbox. Auto-spawns if needed."""
    agent = get_agent()
    return agent.execute(req.slack_thread_key, req.message, req.harness, req.repo, req.request_id)


@router.post("/stop")
async def stop(req: StopRequest) -> dict[str, Any]:
    """Stop and remove a sandbox container."""
    agent = get_agent()
    return agent.stop(req.slack_thread_key)


@router.post("/interrupt")
async def interrupt(req: InterruptRequest) -> dict[str, Any]:
    """Interrupt a running command."""
    agent = get_agent()
    return agent.interrupt(req.slack_thread_key)


@router.get("/status")
async def status(key: str | None = None) -> dict[str, Any]:
    """Get session status. If no key given, list all."""
    agent = get_agent()
    return agent.status(key)


@router.get("/pool")
async def pool() -> dict[str, Any]:
    """Show pool status."""
    agent = get_agent()
    return agent.pool()
