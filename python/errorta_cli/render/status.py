"""Run status view (``GET /healthz`` + ``GET /run``) — spec §9 "Run status".

Surfaces sidecar health + the bound project's run ``state`` (status + last
``stop_reason`` + counters). The counters come from ``state.counters`` (a
``LoopCounters`` subset the route persists: iterations, turns_repaired,
model_escalations, task_reassignments, pm_assists).
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import muted, render

_STATUS_STYLE = {
    "running": "cli.warn",
    "stopped": "cli.muted",
    "interrupted": "cli.bad",
    "failed": "cli.bad",
    "idle": "cli.muted",
}

# stop_reasons that are a genuine failure (vs a clean finish / checkpoint).
_TERMINAL_BAD = {
    "budget_exhausted", "no_progress", "hard_blocker", "member_unhealthy",
    "worker_unproductive", "completion_blocked", "not_converging",
}


def render_status(payload: Any, verbosity: Any) -> str:
    health = (payload or {}).get("health") or {}
    lines: list[Any] = []
    lines.append(
        Text(
            "sidecar: {s} v{v} (python {p})".format(
                s=health.get("service", "?"),
                v=health.get("version", "?"),
                p=health.get("python", "?"),
            )
        )
    )
    build = health.get("build") or {}
    if build.get("commit"):
        dirty = " (dirty)" if build.get("dirty") else ""
        lines.append(muted(f"build:   {build.get('commit')}{dirty}"))
    residency = health.get("residency") or {}
    mode = residency.get("mode") or residency.get("residency")
    if mode:
        lines.append(muted(f"residency: {mode}"))

    pid = (payload or {}).get("project_id")
    if not pid:
        lines.append(muted("project: (none bound to this directory)"))
        return render(*lines)

    lines.append(Text(f"project: {pid}", style="cli.key"))
    run = (payload or {}).get("run") or {}
    state = run.get("state") or {}
    running = run.get("running")
    status = "running" if running else str(state.get("status") or "idle")
    lines.append(Text(f"run:     {status}", style=_STATUS_STYLE.get(status, "white")))

    stop_reason = state.get("stop_reason")
    if stop_reason:
        style = "cli.bad" if stop_reason in _TERMINAL_BAD else "cli.muted"
        lines.append(Text(f"stop:    {stop_reason}", style=style))
    if run.get("can_resume"):
        lines.append(muted("         (resumable)"))
    if state.get("last_error"):
        lines.append(Text(f"error:   {state.get('last_error')}", style="cli.bad"))

    counters = state.get("counters") or {}
    if counters:
        table = Table(show_edge=False, pad_edge=False, box=None, show_header=False)
        table.add_column("k", style="cli.muted", no_wrap=True)
        table.add_column("v", justify="right", no_wrap=True)
        for key in ("iterations", "model_calls", "tasks_done", "turns_repaired",
                    "model_escalations", "task_reassignments", "pm_assists"):
            if counters.get(key) is not None:
                table.add_row(f"  {key}", str(counters.get(key)))
        lines.append(table)
    return render(*lines)
