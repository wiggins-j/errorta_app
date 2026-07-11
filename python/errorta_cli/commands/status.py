"""``status`` — sidecar health + the bound project's run state (spec §9).

Calls ``GET /healthz`` and, when a project is bound to the cwd, ``GET
/coding/projects/{id}/run`` (state + last ``stop_reason`` + counters). Works
identically as ``errorta status [--json]`` and ``/status`` in the REPL because
both front-ends dispatch through the shared registry.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, register
from ..render.status import render_status
from ..session import Context
from ._base import make_render


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    health = client.get_json("/healthz")
    run: Any = None
    if ctx.project_id:
        # GET /coding/projects/{id}/run is side-effecting (it runs recovery /
        # reconcile). This is safe ONLY because sidecar.resolve() guarantees sole
        # ownership: the CLI adopts its own live sidecar or refuses to spawn a
        # second one next to a foreign app — so this call never hits a foreign
        # sidecar and never corrupts another process's live run.
        run = client.get_json(f"/coding/projects/{ctx.project_id}/run")
    return {"project_id": ctx.project_id, "health": health, "run": run}


_render = make_render(render_status)


register(
    Command(
        name="status",
        help="Show sidecar health and the bound project's run state.",
        call=_call,
        render=_render,
        params=(),
    )
)
