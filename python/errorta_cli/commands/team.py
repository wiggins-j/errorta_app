"""``team`` — resolved members from the run-config projection (``GET /model-usage``).

A coding project stores no room, only a ``run_config`` of members; ``model-usage``
is the only read-only route that surfaces that composition (single/multi + routes/
pools). Full member editing is S4.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, register
from ..render.team import render_team
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    return client.get_json(f"/coding/projects/{ctx.project_id}/model-usage")


register(
    Command(
        name="team",
        help="Resolved team members (id, role, mode, route/pool).",
        call=_call,
        render=_base.make_render(render_team),
        params=(),
    )
)
