"""``pm chat`` / ``pm changes`` — read-only PM surfaces.

``pm chat`` → ``GET /pm-chat``; ``pm changes`` → ``GET /pm-changes``. Both routes
are TAURI-guarded GETs — the client sends the origin header on every request, so
they work unchanged. Applying/accept/decline is S6.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register
from ..render.pm import render_pm
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    sub = str(args.get("sub") or "chat").lower()
    if sub == "changes":
        payload = dict(client.get_json(f"/coding/projects/{ctx.project_id}/pm-changes") or {})
        payload["_sub"] = "changes"
        return payload
    if sub not in ("chat",):
        return _base.usage("pm <chat|changes>")
    payload = dict(client.get_json(f"/coding/projects/{ctx.project_id}/pm-chat") or {})
    payload["_sub"] = "chat"
    return payload


register(
    Command(
        name="pm",
        help="PM read views: `pm chat` (history) or `pm changes` (pending/recent).",
        call=_call,
        render=_base.make_render(render_pm),
        params=(Param("sub", "chat | changes", default="chat"),),
    )
)
