"""``watch`` — one live run dashboard (Spec 06).

Client-side composition (NO new server route). Each poll tick GETs ``/run``,
``/usage-summary``, ``/test-runs``, ``/team-log`` and ``/turns?limit=`` and merges
them into a single snapshot; :func:`~errorta_cli.render.watch.render_watch` draws
one compact panel (run status + caps, token total, gate pass-count, per-member
activity, last event, and a convergence indicator).

``errorta watch`` refreshes live on the shared poll harness (the same
``run_watch`` loop ``log``/``tokens`` use for ``--watch`` — no new threading
model): the front-ends arm it by default via ``watch.arm_dashboard``. ``--once``
renders a single snapshot (scriptable); ``--interval N`` sets the tick (default
2s). Guards the project binding with ``_base.no_project()`` when unbound, so it
never builds a ``.../None`` route.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register
from ..render.watch import render_watch
from ..session import Context
from . import _base

# `/turns?limit=` window for the "current member activity" fallback.
_TURNS_LIMIT = 6


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    base = f"/coding/projects/{ctx.project_id}"
    # Compose the existing read routes into one snapshot. `render_watch` reads each
    # sub-payload by its route-native shape; nothing new is persisted server-side.
    return {
        "run": client.get_json(f"{base}/run"),
        "usage": client.get_json(f"{base}/usage-summary"),
        "test_runs": client.get_json(f"{base}/test-runs"),
        "team_log": client.get_json(f"{base}/team-log"),
        "turns": client.get_json(f"{base}/turns", params={"limit": _TURNS_LIMIT}),
    }


register(
    Command(
        name="watch",
        help="Live run dashboard (run/tokens/gate/members in one auto-refreshing panel).",
        call=_call,
        render=_base.make_render(render_watch),
        params=(
            Param("once", "render a single snapshot instead of looping (scriptable)",
                  is_flag=True),
            Param("interval", "seconds between refreshes (default 2)"),
            Param("watch", "re-render on the poll loop", is_flag=True),
        ),
    )
)
