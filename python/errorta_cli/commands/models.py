"""``models`` — "what the PM learned" (``GET /model-learning`` + ``GET /model-usage``).

model-learning is global (cross-project); model-usage is this project's assignment
rollup (surfaces escalations the app under-uses).
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, register
from ..render.models import render_models
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    learning = (client.get_json("/coding/model-learning") or {}).get("learning") or {}
    usage: dict[str, Any] = {}
    if _base.has_project(ctx):
        usage = (client.get_json(
            f"/coding/projects/{ctx.project_id}/model-usage"
        ) or {}).get("usage") or {}
    return {"learning": learning, "usage": usage}


register(
    Command(
        name="models",
        help="What the PM learned (cross-project) + this project's assignments.",
        call=_call,
        render=_base.make_render(render_models),
        params=(),
    )
)
