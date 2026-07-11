"""``tasks`` (compact table) + ``board`` (columns) — both ``GET /backlog``.

A full-screen Textual board (``/board --tui``) is explicitly out of S2; ``board``
here is a plain Rich columns view.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register
from ..render.board import render_board, render_tasks
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    return client.get_json(f"/coding/projects/{ctx.project_id}/backlog")


register(
    Command(
        name="tasks",
        help="Backlog as a compact status table.",
        call=_call,
        render=_base.make_render(render_tasks),
        params=(Param("watch", "re-render on the poll loop", is_flag=True),),
    )
)

register(
    Command(
        name="board",
        help="Backlog as todo/doing/blocked/done columns.",
        call=_call,
        render=_base.make_render(render_board),
        params=(Param("watch", "re-render on the poll loop", is_flag=True),),
    )
)
