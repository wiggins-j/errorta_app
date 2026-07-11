"""Rich rendering for the S3 run-control views (F147 §8, §9).

Covers: the ``setup`` readiness view (``GET /run-setup``), the preflight unhealthy
list (``POST /run-setup/preflight``), the single-event lines the live ``run``
stream prints, and the terminal / detach / aborted summaries the ``run`` command
returns. Renderers SELECT fields (no raw-payload dump — invariant #4/#5); the raw
bytes are only reachable via ``--json``.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import muted, render, role_style, ts

# --------------------------------------------------------------------------- #
# Live-stream single-event lines.
# --------------------------------------------------------------------------- #

def render_stream_event(event: Any) -> str | None:
    """Format ONE synthesized poll event into a live-view line (or None to skip).

    ``event`` is an :class:`~errorta_cli.poller.Event` — ``channel`` names the
    ledger it came from and ``item`` is the raw entry. We surface the salient,
    enumerated fields per channel only.
    """
    channel = getattr(event, "channel", "")
    item = getattr(event, "item", None)
    if not isinstance(item, dict):
        # Snapshot "changed" events (tokens/runtime) carry the whole payload — a
        # low-signal firehose note; skip unless a specific renderer wants it.
        return None

    if channel == "team-log":
        role = str(item.get("role") or "system")
        member = str(item.get("member") or "")
        who = role if not member else f"{role}:{member}"
        line = Text()
        line.append(f"{ts(item.get('at')):>8} ", style="cli.muted")
        line.append(f"{who:<16} ", style=role_style(role))
        kind = str(item.get("kind") or "")
        if kind:
            line.append(f"[{kind}] ", style="cli.muted")
        line.append(str(item.get("message") or ""))
        return render(line)

    if channel == "decisions":
        line = Text()
        line.append(f"{ts(item.get('at')):>8} ", style="cli.muted")
        line.append("· ", style="cli.muted")
        line.append(str(item.get("choice") or ""), style="cli.key")
        title = str(item.get("title") or "")
        if title:
            line.append(f" — {title}")
        return render(line)

    if channel == "turns":
        role = str(item.get("role") or "")
        line = Text()
        line.append(f"{ts(item.get('at')):>8} ", style="cli.muted")
        line.append(f"turn {role}", style=role_style(role))
        member = str(item.get("member_id") or "")
        if member:
            line.append(f":{member}", style=role_style(role))
        outcome = str(item.get("outcome") or "")
        if outcome:
            line.append(f" → {outcome}", style="cli.muted")
        return render(line)

    if channel == "prs":
        line = Text("PR ", style="cli.muted")
        line.append(str(item.get("pr_id") or ""), style="cli.key")
        line.append(f" {item.get('status') or ''}")
        branch = str(item.get("branch") or "")
        if branch:
            line.append(f" ({branch})", style="cli.muted")
        return render(line)

    if channel == "attention":
        blocking = " BLOCKING" if item.get("blocking") else ""
        line = Text("attention", style="cli.warn")
        line.append(f"{blocking}: ", style="cli.bad" if blocking else "cli.warn")
        line.append(str(item.get("title") or item.get("kind") or ""))
        return render(line)

    if channel == "tools":
        line = Text("tool ", style="cli.muted")
        line.append(str(item.get("tool") or ""), style="cli.key")
        intent = str(item.get("intent") or "")
        if intent:
            line.append(f" {intent}", style="cli.muted")
        line.append(f" → {item.get('status') or ''}")
        return render(line)

    return None


# --------------------------------------------------------------------------- #
# Preflight (member-health) list.
# --------------------------------------------------------------------------- #

def render_preflight(unhealthy: list[Any]) -> str:
    """Render the ``{unhealthy:[{provider,route,reason,remediation}]}`` list."""
    if not unhealthy:
        return render(Text("preflight: every required provider is ready", style="cli.ok"))
    lines: list[Any] = [Text("preflight: providers not ready", style="cli.bad")]
    for entry in unhealthy:
        if not isinstance(entry, dict):
            continue
        head = Text("  • ", style="cli.bad")
        head.append(str(entry.get("provider") or "?"), style="cli.key")
        route = str(entry.get("route") or "")
        if route:
            head.append(f" ({route})", style="cli.muted")
        head.append(f"  {entry.get('reason') or ''}", style="cli.warn")
        lines.append(head)
        remediation = str(entry.get("remediation") or "")
        if remediation:
            lines.append(muted(f"      → {remediation}"))
    return render(*lines)


# --------------------------------------------------------------------------- #
# Setup readiness view (GET /run-setup).
# --------------------------------------------------------------------------- #

def render_setup(payload: Any) -> str:
    """Render the readiness gate state: confirmed flag + governance/autonomy/guardrail."""
    data = payload or {}
    confirmed = bool(data.get("run_setup_confirmed"))
    lines: list[Any] = []
    lines.append(
        Text(
            f"run setup: {'confirmed' if confirmed else 'NOT confirmed'}",
            style="cli.ok" if confirmed else "cli.warn",
        )
    )
    if not confirmed:
        lines.append(muted("  confirm before the first run: errorta setup --confirm --yes"))

    governance = data.get("governance") or {}
    gstate = governance.get("state") if isinstance(governance.get("state"), dict) else governance
    table = Table(show_edge=False, pad_edge=False, box=None, show_header=False)
    table.add_column("k", style="cli.muted", no_wrap=True)
    table.add_column("v", no_wrap=False)
    for label, value in (
        ("governance mode", gstate.get("mode")),
        ("governance phase", gstate.get("phase")),
        ("human code approval", gstate.get("human_code_approval")),
        ("max review rounds", gstate.get("max_review_rounds")),
        ("block on problems", gstate.get("block_on_problems")),
    ):
        if value is not None:
            table.add_row(f"  {label}", str(value))

    autonomy = data.get("autonomy") or {}
    for label, key in (
        ("max iterations", "max_iterations"),
        ("max model calls", "max_model_calls"),
        ("max parallel workers", "max_parallel_workers"),
        ("checkpoint cadence", "checkpoint_cadence"),
        ("checkpoint n", "checkpoint_n"),
        ("member failure limit", "member_failure_limit"),
    ):
        if autonomy.get(key) is not None:
            table.add_row(f"  {label}", str(autonomy.get(key)))

    table.add_row("  guardrail enabled", str(bool(data.get("guardrail_enabled"))))
    table.add_row("  member-health preflight", str(bool(data.get("member_health_preflight"))))
    lines.append(table)
    return render(*lines)


# --------------------------------------------------------------------------- #
# Run terminal / detach / aborted / started summaries.
# --------------------------------------------------------------------------- #

def render_run_terminal(run_payload: Any, *, reason: str | None, gloss_text: str) -> str:
    """One-line-ish summary of a finished run + its counters."""
    from ..errors import EXIT_RUN_FAILED
    from ..runstream import classify_exit

    state = (run_payload or {}).get("state") or {}
    failed = classify_exit(run_payload) == EXIT_RUN_FAILED
    style = "cli.bad" if failed else "cli.ok"
    head = "run failed" if failed else "run finished"
    lines: list[Any] = [Text(f"{head}: {reason or 'done'} — {gloss_text}", style=style)]
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


def render_started(project_id: str, *, detached: bool) -> str:
    """Confirmation line for a fired-and-not-streamed run."""
    if detached:
        return render(Text(
            f"run started (detached) for {project_id}. "
            "Track it: errorta status  /  errorta log --watch"
        ))
    return render(Text(f"run started for {project_id}."))


def render_detached(project_id: str) -> str:
    """The Ctrl-C detach note — the run keeps going in the background."""
    return render(muted(
        f"detached — the run continues in the background for {project_id}. "
        "errorta status to check, errorta cancel to stop it."
    ))
