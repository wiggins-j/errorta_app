"""``decisions`` — the raw ``choice`` event stream (``GET /decisions``).

``--kind`` filters by the decision ``choice`` and supports globs (``pr_*``).
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register
from ..render.decisions import render_decisions
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    payload = dict(client.get_json(f"/coding/projects/{ctx.project_id}/decisions") or {})
    if args.get("kind"):
        payload["_kind"] = str(args["kind"])
    return payload


register(
    Command(
        name="decisions",
        help="Decision event stream (--kind supports globs, e.g. pr_*).",
        call=_call,
        render=_base.make_render(render_decisions),
        params=(
            Param("kind", "filter by choice (glob ok, e.g. pr_*)"),
            Param("watch", "tail via the poll loop", is_flag=True),
        ),
    )
)
