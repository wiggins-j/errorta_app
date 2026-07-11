"""``tokens`` — F143 usage rollup with a measured-vs-estimated meter.

``GET /usage-summary`` → ``by_role``/``by_route``/``by_member``/``total``.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register
from ..render.tokens import render_tokens
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    return client.get_json(f"/coding/projects/{ctx.project_id}/usage-summary")


register(
    Command(
        name="tokens",
        help="Token usage rollup (by role/route/member; measured-vs-estimated).",
        call=_call,
        render=_base.make_render(render_tokens),
        params=(Param("watch", "re-render on the poll loop", is_flag=True),),
    )
)
