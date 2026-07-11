"""``attention`` — problems/alerts (``GET /attention`` → signals + blocks_stage)."""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register
from ..render.attention import render_attention
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    params = {}
    if args.get("state"):
        params["state"] = args["state"]
    return client.get_json(
        f"/coding/projects/{ctx.project_id}/attention", params=params or None
    )


register(
    Command(
        name="attention",
        help="Problems + alerts (blocking flag, stage).",
        call=_call,
        render=_base.make_render(render_attention),
        params=(
            Param("state", "filter by state (e.g. open)"),
            Param("watch", "re-render on the poll loop", is_flag=True),
        ),
    )
)
