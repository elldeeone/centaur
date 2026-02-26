"""Thread viewer API — GET endpoints for the thread viewer UI."""

from __future__ import annotations

import sys
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api.deps import verify_api_key

router = APIRouter(
    prefix="/threads",
    tags=["threads"],
    dependencies=[Depends(verify_api_key)],
)


def _get_sessions() -> dict[str, Any]:
    """Access the agent plugin's session registry via the loaded module."""
    mod = sys.modules.get("shared.plugins_runtime.agent.client")
    if mod:
        return getattr(mod, "_sessions", {})
    return {}


@router.get("")
async def list_threads() -> dict[str, Any]:
    """List all active agent threads with summary info."""
    sessions = _get_sessions()
    threads = []
    for key, session in sessions.items():
        turns = session.get("turns", [])
        threads.append(
            {
                "slack_thread_key": key,
                "container_id": session["container_id"][:12],
                "harness": session["harness"],
                "agent_thread_id": session.get("agent_thread_id"),
                "state": session["state"],
                "created_at": session["created_at"],
                "last_activity": session["last_activity"],
                "turn_count": len(turns),
                "last_result": turns[-1]["result"][:200] if turns else "",
            }
        )
    return {"threads": threads, "count": len(threads)}


@router.get("/detail")
async def get_thread(key: str) -> dict[str, Any]:
    """Get full event stream for a specific thread."""
    sessions = _get_sessions()
    session = sessions.get(key)
    if not session:
        raise HTTPException(status_code=404, detail=f"Thread '{key}' not found")
    return {
        "slack_thread_key": key,
        "container_id": session["container_id"][:12],
        "harness": session["harness"],
        "agent_thread_id": session.get("agent_thread_id"),
        "state": session["state"],
        "created_at": session["created_at"],
        "last_activity": session["last_activity"],
        "turns": session.get("turns", []),
    }
