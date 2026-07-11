"""``status`` — the one real read command proving the S1 spine.

Calls ``GET /healthz`` and, when a project is bound to the cwd, ``GET
/coding/projects/{id}/run``, then renders a terminal summary (spec §9 "Run
status"). Works identically as ``errorta status [--json]`` and ``/status`` in the
REPL because both front-ends dispatch through the shared registry.

Rich rendering polish and the other read views are S2; S1 keeps ``status``
deliberately simple.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, register, render_json
from ..session import Context
from ..verbosity import Verbosity


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


def _render(payload: Any, verbosity: Verbosity, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)

    health = payload.get("health") or {}
    lines: list[str] = []
    lines.append(
        "sidecar: {service} v{version} (python {py})".format(
            service=health.get("service", "?"),
            version=health.get("version", "?"),
            py=health.get("python", "?"),
        )
    )

    build = health.get("build") or {}
    commit = build.get("commit")
    if commit:
        dirty = " (dirty)" if build.get("dirty") else ""
        lines.append(f"build:   {commit}{dirty}")

    residency = health.get("residency") or {}
    mode = residency.get("mode") or residency.get("residency")
    if mode:
        lines.append(f"residency: {mode}")

    pid = payload.get("project_id")
    if not pid:
        lines.append("project: (none bound to this directory)")
        return "\n".join(lines)

    lines.append(f"project: {pid}")
    run = payload.get("run") or {}
    state = run.get("state") or {}
    running = run.get("running")
    run_status = state.get("status") or run.get("result") or "unknown"
    lines.append(f"run:     {'running' if running else run_status}")
    stop_reason = state.get("stop_reason")
    if stop_reason:
        lines.append(f"stop:    {stop_reason}")
    if run.get("can_resume"):
        lines.append("         (resumable)")
    return "\n".join(lines)


register(
    Command(
        name="status",
        help="Show sidecar health and the bound project's run state.",
        call=_call,
        render=_render,
        params=(),
    )
)
