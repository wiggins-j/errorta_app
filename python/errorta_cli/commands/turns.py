"""``turns`` (transcript list) + ``turn <task> <turn>`` (detail + Context Report).

``turns`` → ``GET /turns?limit=``. ``turn`` matches the turn then fetches its
``.../tasks/{task}/turns/{turn}/composition`` (the per-turn Context Report).
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register
from ..render.turns import render_turn_detail, render_turns
from ..session import Context
from . import _base

_DEFAULT_LIMIT = 100


def _call_list(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    limit = args.get("limit") or _DEFAULT_LIMIT
    return client.get_json(
        f"/coding/projects/{ctx.project_id}/turns", params={"limit": limit}
    )


def _call_detail(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    task_id = args.get("task")
    turn_id = args.get("turn")
    if not task_id or not turn_id:
        return _base.usage("turn <task> <turn>")
    turns = (client.get_json(
        f"/coding/projects/{ctx.project_id}/turns", params={"limit": 1000}
    ) or {}).get("turns") or []
    matched = next((t for t in turns if str(t.get("turn_id")) == str(turn_id)), None)
    composition = client.get_json(
        f"/coding/projects/{ctx.project_id}/tasks/{task_id}/turns/{turn_id}/composition"
    )
    return {"turn": matched, "composition": composition}


register(
    Command(
        name="turns",
        help="Per-turn transcript (role/route/outcome/tokens).",
        call=_call_list,
        render=_base.make_render(render_turns),
        params=(
            Param("limit", "max turns", default=_DEFAULT_LIMIT),
            Param("watch", "re-render on the poll loop", is_flag=True),
        ),
    )
)

register(
    Command(
        name="turn",
        help="One turn's transcript + Context Report.",
        call=_call_detail,
        render=_base.make_render(render_turn_detail),
        params=(
            Param("task", "task id", required=True),
            Param("turn", "turn id", required=True),
        ),
    )
)
