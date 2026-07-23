"""``gate`` — acceptance/test-run gate status (Spec 03).

``GET /coding/projects/{id}/test-runs`` → ``{runs:[{at, passed, head, sandbox,
command_ids, results:[{command_id, status, exit_code}]}]}``. Surfaces the latest
verdict, the failing command ids, and the pass-count trend so an operator can see
"how close is the gate" (and whether it is stuck) without hand-tailing the ledger.

Mirrors ``tokens.py``: a project-bound GET with a no-project guard, ``--json`` for
scripting, and ``--watch`` (snapshot re-render) via the shared poll loop.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register
from ..render.gate import render_gate
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    return client.get_json(f"/coding/projects/{ctx.project_id}/test-runs")


register(
    Command(
        name="gate",
        help="Show acceptance/test gate status (latest verdict, failing commands, trend).",
        call=_call,
        render=_base.make_render(render_gate),
        params=(Param("watch", "re-render on the poll loop", is_flag=True),),
    )
)
