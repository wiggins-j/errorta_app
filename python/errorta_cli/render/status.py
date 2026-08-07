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

from . import NO_PROJECT_MSG, muted, render
from .project import status_cell, status_label

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
    "delivery_review_stalled", "revise_livelock",  # Spec 16 (Δ2: mine only)
    # Spec 18 (Δ2): Specs 04/07/10 added these stop reasons without updating the set.
    "gate_not_improving", "planning_churn", "dispatch_wedged",
}

# Spec 18: the unbound `status` view. A bounded, run-focused slice of the project
# list — running first, then blocking attention, then the rest — capped so it
# stays a status view rather than a second `errorta projects`.
_UNBOUND_CAP = 5
# list_status_reasons that add nothing over the status label itself, so the row
# omits them (a live run / a plain lifecycle state need no extra explanation).
_UNINFORMATIVE_REASONS = {"", "live_run", "lifecycle", "blocking_attention"}


def _project_sort_key(proj: dict[str, Any]) -> int:
    label = status_label(proj)
    if label == "running":
        return 0
    if label == "needs attention":
        return 1
    return 2


def _unbound_project_row(proj: dict[str, Any]) -> Text:
    # `status_cell` is shared with `errorta projects` (no wording drift), and its
    # list-status coloring already renders a failed/attention run in the failure
    # style — the deriver classifies those as "needs attention" (cli.bad). We do
    # NOT overlay `_TERMINAL_BAD` here: `list_status_reason` is a derived category
    # label (live_run / blocking_attention / lifecycle / …), never a raw stop
    # reason, so such an overlay would never fire on a real payload (review Δ).
    cell = status_cell(proj)
    reason = str(proj.get("list_status_reason") or "")
    row = Text()
    row.append(f"{str(proj.get('id') or '?'):<16} ", style="cli.key")
    row.append_text(cell)
    if reason and reason not in _UNINFORMATIVE_REASONS:
        row.append(f"  {reason}", style="cli.muted")
    return row


def _unbound_project_lines(projects: Any) -> list[Any]:
    """The actionable project slice printed when nothing is bound to the cwd.

    Empty when there are no projects — an empty table is noise, so the caller
    prints just the no-project message.
    """
    items = [p for p in (projects or []) if isinstance(p, dict)]
    if not items:
        return []
    ordered = sorted(items, key=_project_sort_key)  # stable: bucket order within
    shown = ordered[:_UNBOUND_CAP]
    lines: list[Any] = [Text("")]
    lines.extend(_unbound_project_row(proj) for proj in shown)
    extra = len(ordered) - len(shown)
    if extra > 0:
        lines.append(muted(f"(+{extra} more — errorta projects)"))
    lines.append(Text(""))
    lines.append(muted("errorta open <id>      bind this directory"))
    lines.append(muted("errorta watch          live dashboard for the bound project"))
    return lines


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
    # Spec 22 Item 1: where the sidecar's stdout/stderr land. This is the line
    # that turns "a 500 with no greppable content" into `grep <error id> <path>`.
    log_path = (payload or {}).get("log_path")
    if log_path:
        lines.append(muted(f"log:     {log_path}"))

    pid = (payload or {}).get("project_id")
    if not pid:
        lines.append(muted(NO_PROJECT_MSG))
        lines.extend(_unbound_project_lines((payload or {}).get("projects")))
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

    # Spec 01: effective run caps + a (default) marker for any cap that fell back
    # to the built-in default (absent from autonomy.json). Additive/backward
    # compatible: an older server omits `caps`, so nothing is printed.
    caps = run.get("caps") or {}
    if caps:
        defaulted = set(caps.get("defaulted") or [])

        def _cap(key: str, value: Any) -> str:
            if key == "max_model_calls" and value is None:
                shown = "∞"          # unlimited
            elif key == "max_parallel_workers" and value is None:
                shown = "auto"            # AUTO — bounded by worker count
            else:
                shown = str(value)
            return f"{shown} (default)" if key in defaulted else shown

        lines.append(muted(
            "caps: iterations {i}  model_calls {m}  parallel {p}"
            "  delivery_rounds {d}".format(
                i=_cap("max_iterations", caps.get("max_iterations")),
                m=_cap("max_model_calls", caps.get("max_model_calls")),
                p=_cap("max_parallel_workers", caps.get("max_parallel_workers")),
                d=_cap("delivery_review_round_limit",
                       caps.get("delivery_review_round_limit")),
            )
        ))

    # Spec 10 §4: `todo: N (dispatchable: M)`. When M == 0 and N is large the
    # backlog is wedged — the one line that makes it diagnosable at a glance.
    # Additive/backward compatible: an older server omits `backlog`, so nothing is
    # printed. Rendered in the bad style when there is todo work but none of it is
    # dispatchable (the wedge signature).
    backlog = run.get("backlog") or {}
    if backlog:
        todo_n = backlog.get("todo")
        dispatchable = backlog.get("dispatchable")
        if todo_n is not None and dispatchable is not None:
            wedged = int(todo_n) > 0 and int(dispatchable) == 0
            style = "cli.bad" if wedged else "cli.muted"
            lines.append(Text(
                f"todo:    {todo_n} (dispatchable: {dispatchable})", style=style))
        blocked_cap = backlog.get("blocked_on_capability")
        if blocked_cap:
            lines.append(Text(
                f"blocked: {blocked_cap} on a missing capability (auto-retry when enabled)",
                style="cli.warn"))

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
