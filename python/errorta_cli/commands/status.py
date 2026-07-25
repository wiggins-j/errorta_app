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
    projects: Any = None
    if ctx.project_id:
        # GET /coding/projects/{id}/run is side-effecting (it runs recovery /
        # reconcile). This is safe ONLY because sidecar.resolve() guarantees sole
        # ownership: the CLI adopts its own live sidecar or refuses to spawn a
        # second one next to a foreign app — so this call never hits a foreign
        # sidecar and never corrupts another process's live run. The bound branch
        # therefore makes exactly one run call and nothing else (Spec 18: untouched).
        run = client.get_json(f"/coding/projects/{ctx.project_id}/run")
    else:
        # Nothing bound: surface what the sidecar is actually doing (Spec 18).
        # GET /coding/projects is a plain read with no origin guard, so it is
        # safe from any directory. Guarded — `status` is the command an operator
        # reaches for when things are broken, so a failing list must degrade to
        # the health-only payload rather than turning a health check into an error.
        try:
            resp = client.get_json("/coding/projects") or {}
            projects = resp.get("projects") or []
        except Exception:
            projects = None
    return {
        "project_id": ctx.project_id,
        "health": health,
        "run": run,
        "projects": projects,
    }


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
