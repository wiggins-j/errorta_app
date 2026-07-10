"""F045 — tool catalog + MCP health routes.

- ``GET /tools/catalog`` — full tool catalog (settings display).
- ``GET /tools/catalog?room_id=X`` — only the tools that room's policy grants
  AND that are configured (a disabled/un-granted tool is never listed as
  available).
- ``GET /tools/mcp/health`` — per-server health + circuit-breaker state,
  queryable WITHOUT invoking any tool.

These read-only routes power Settings -> Tools and the F046 work rail. MCP
clients stay under ``errorta_tools``; this module only reads catalog metadata
and health state.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from errorta_tools import catalog
from errorta_tools.mcp_state import McpStateRegistry

router = APIRouter(prefix="/tools", tags=["tools"])

# Process-level MCP state registry. MCP servers are default-off; until a server
# is configured this stays empty and /tools/mcp/health returns []. The slice
# that wires real MCP config seeds this registry.
_MCP_STATE = McpStateRegistry()


def mcp_state() -> McpStateRegistry:
    return _MCP_STATE


def _room_tool_policy(room_id: str) -> dict[str, Any]:
    from errorta_council import paths as council_paths
    from errorta_council.room_store import RoomNotFound, RoomStore

    store = RoomStore(
        rooms_dir=council_paths.rooms_dir(),
        deleted_dir=council_paths.rooms_dir() / "deleted",
    )
    try:
        room = store.get(room_id)
    except RoomNotFound:
        raise HTTPException(status_code=404, detail="room not found")
    raw = room.to_dict()
    tp = raw.get("tool_policy")
    return tp if isinstance(tp, dict) else {}


@router.get("/catalog")
def tool_catalog(room_id: str | None = Query(default=None)) -> dict[str, Any]:
    if room_id is None:
        return {"tools": [m.to_dict() for m in catalog.all_metadata()]}
    tool_policy = _room_tool_policy(room_id)
    granted = catalog.filter_for_room(tool_policy=tool_policy)
    return {
        "room_id": room_id,
        "tools": [m.to_dict() for m in granted],
    }


@router.get("/mcp/health")
def mcp_health() -> dict[str, Any]:
    return {"servers": [h.to_dict() for h in mcp_state().all()]}
