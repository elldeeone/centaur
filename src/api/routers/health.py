from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.deps import verify_operator_api_key

router = APIRouter()


@router.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok"}


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready() -> dict:
    return {"status": "ok"}


@router.get("/health/tools", dependencies=[Depends(verify_operator_api_key)])
async def health_tools() -> dict[str, Any]:
    from api.app import get_tool_manager

    tool_manager = get_tool_manager()
    loaded = [
        {"name": tool.name, "methods": sorted(method.method_name for method in tool.methods)}
        for tool in tool_manager.tools.values()
    ]
    failed = list(tool_manager.load_failures)
    return {
        "loaded": loaded,
        "failed": failed,
        "summary": {
            "loaded_count": len(loaded),
            "failed_count": len(failed),
            "total_methods": sum(len(item["methods"]) for item in loaded),
        },
    }
