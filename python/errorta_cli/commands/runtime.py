"""``runtime`` — read-only runtime profiles view (``GET /runtime/profiles``).

``--session <sid>`` also fetches ``GET /runtime/sessions/{sid}``. Runtime control
(detect/setup/start/stop) is S7.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..errors import NotFound
from ..registry import Command, Param, register
from ..render.runtime import render_runtime
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    payload = dict(client.get_json(f"/coding/projects/{ctx.project_id}/runtime/profiles") or {})
    sid = args.get("session")
    if sid:
        try:
            session = client.get_json(
                f"/coding/projects/{ctx.project_id}/runtime/sessions/{sid}"
            )
            payload["session"] = (session or {}).get("session")
        except NotFound:
            payload["session"] = None
    return payload


register(
    Command(
        name="runtime",
        help="Runtime profiles (read-only); --session <sid> for a live session.",
        call=_call,
        render=_base.make_render(render_runtime),
        params=(
            Param("session", "session id to inspect"),
            Param("watch", "re-render on the poll loop", is_flag=True),
        ),
    )
)
