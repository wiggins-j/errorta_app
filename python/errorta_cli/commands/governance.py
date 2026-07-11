"""``governance`` — read-only governance view (``GET /governance``).

Approvals/settings are S6.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, register
from ..render.governance import render_governance
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    return client.get_json(f"/coding/projects/{ctx.project_id}/governance")


register(
    Command(
        name="governance",
        help="Governance state (mode/phase/stage) — read-only.",
        call=_call,
        render=_base.make_render(render_governance),
        params=(),
    )
)
