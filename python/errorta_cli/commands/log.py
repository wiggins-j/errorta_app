"""``log`` — Team Log narrative (``GET /coding/projects/{id}/team-log``).

Colorized by role; filters ``--role``/``--member``/``--grep`` (applied at render
so ``--json`` stays the raw route payload plus the applied ``_filters``);
``--watch`` tails via the poll loop (handled by the front-end).
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register
from ..render.log import render_log
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    payload = dict(client.get_json(f"/coding/projects/{ctx.project_id}/team-log") or {})
    payload["_filters"] = {
        "role": args.get("role"),
        "member": args.get("member"),
        "grep": args.get("grep"),
    }
    return payload


register(
    Command(
        name="log",
        help="Team Log narrative (colorized by role; --watch tails live).",
        call=_call,
        render=_base.make_render(render_log),
        watch_mode="stream",  # F151: --watch appends new events (tail -f), no repaint
        params=(
            Param("role", "filter by role (pm/dev/reviewer/tester)"),
            Param("member", "filter by member id substring"),
            Param("grep", "filter by message substring"),
            Param("watch", "tail via the poll loop", is_flag=True),
        ),
    )
)
