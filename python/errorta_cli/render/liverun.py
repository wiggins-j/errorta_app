"""Rich rendering for the live-run operator views (``/liverun/*``).

Three shapes, all field-selected (golden invariant #5 — the raw payload is only
reachable through ``--json``):

* the profile list, with the failing rule on anything that does not validate;
* the run status: phase, why, elapsed, per-probe last-ok age, caps headroom,
  fix-loop headroom, and the two operator holds — the same view ``--watch``
  repaints each tick;
* the short verdict lines the mutations return.

The probe ages are the load-bearing number here. A live run's health is not its
phase — a supervisor sits in ``watching`` right up until the stall fires — it is
how long ago each watch probe last answered, measured against that probe's own
``stall_after_s``. So an age is always rendered *with* its budget, and coloured
by how close it is to it.
"""
from __future__ import annotations

from typing import Any

from rich.text import Text

from . import heading, muted, render, truncate

# Phases from which nothing more will happen without an operator.
TERMINAL_PHASES = frozenset({"stopped", "failed", "paused_awaiting_human", "lost_on_restart"})

# Phase → style. Anything unlisted renders unstyled rather than guessing.
_PHASE_STYLE = {
    "idle": "cli.muted",
    "launching": "cli.warn",
    "watching": "cli.ok",
    "stopping": "cli.warn",
    "fixing": "cli.warn",
    "accepting": "cli.warn",
    "deploying": "cli.warn",
    "stopped": "cli.muted",
    "failed": "cli.bad",
    "paused_awaiting_human": "cli.bad",
    "lost_on_restart": "cli.bad",
}

# Fraction of a probe's stall budget that is already "getting warm" / "nearly out".
_WARN_AT = 0.5
_BAD_AT = 0.8


def is_terminal(payload: Any) -> bool:
    """Is there nothing left to watch? A terminal phase, or no live run at all."""
    if not isinstance(payload, dict):
        return True
    if str(payload.get("status") or "") == "empty":
        return True
    return str(payload.get("phase") or "") in TERMINAL_PHASES


def _duration(seconds: Any) -> str:
    """``93.4`` → ``1m33s``. Blank for a missing value (never ``0s``: a probe
    that has never answered is not a probe that answered a moment ago)."""
    if seconds is None:
        return ""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        total = 0
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _probe_style(age: Any, budget: Any) -> str:
    """Colour a last-ok age by how much of its stall budget it has eaten."""
    try:
        age_f, budget_f = float(age), float(budget)
    except (TypeError, ValueError):
        return "cli.muted"
    if budget_f <= 0:
        return "cli.muted"
    ratio = age_f / budget_f
    if ratio >= _BAD_AT:
        return "cli.bad"
    if ratio >= _WARN_AT:
        return "cli.warn"
    return "cli.ok"


def _probe_bits(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``[(probe id, "12s/90s" | "never", style)]`` in a stable order."""
    probes = payload.get("probes")
    if not isinstance(probes, dict):
        return []
    out: list[tuple[str, str, str]] = []
    for pid in sorted(probes):
        info = probes[pid] if isinstance(probes[pid], dict) else {}
        age = info.get("last_ok_age_s")
        budget = info.get("stall_after_s")
        if age is None:
            out.append((str(pid), "never", "cli.muted"))
            continue
        text = _duration(age)
        if budget:
            text = f"{text}/{_duration(budget)}"
        out.append((str(pid), text, _probe_style(age, budget)))
    return out


def _holds(payload: dict[str, Any]) -> list[str]:
    holds = []
    if payload.get("paused"):
        holds.append("PAUSED (start refused until `errorta liverun resume <profile>`)")
    if payload.get("fix_paused"):
        holds.append("fix loop paused (`errorta liverun fix resume <profile>`)")
    return holds


# --------------------------------------------------------------------------- #
# Views.
# --------------------------------------------------------------------------- #

def render_profiles(payload: Any, _verbosity: Any = None) -> str:
    rows = (payload or {}).get("profiles") or []
    if not rows:
        return render(muted(
            "no live-run profiles — author one under "
            "$ERRORTA_HOME/liverun/profiles/<name>.yaml"))
    parts = [heading("Live-run profiles")]
    for row in rows:
        if not isinstance(row, dict):
            continue
        line = Text()
        line.append(f"  {str(row.get('name') or '?'):<24}", style="cli.key")
        if row.get("valid"):
            line.append("ok", style="cli.ok")
        else:
            line.append("invalid", style="cli.bad")
            reason = str(row.get("error") or "").strip()
            if reason:
                line.append(f"  {reason}", style="cli.muted")
        parts.append(line)
    return render(*parts)


def render_status(payload: Any, _verbosity: Any = None) -> str:
    data = payload if isinstance(payload, dict) else {}
    if str(data.get("status") or "") == "empty":
        parts: list[Any] = [muted("no live run")]
        last = data.get("last")
        if isinstance(last, dict) and last:
            tail = Text()
            tail.append("  last: ", style="cli.muted")
            tail.append(str(last.get("profile_name") or "?"), style="cli.key")
            phase = str(last.get("phase") or "")
            if phase:
                tail.append(f"  {phase}", style=_PHASE_STYLE.get(phase, ""))
            reason = str(last.get("reason") or "").strip()
            if reason:
                tail.append(f"  {truncate(reason, 80)}", style="cli.muted")
            parts.append(tail)
        for hold in _holds(data):
            parts.append(Text(f"  {hold}", style="cli.bad"))
        return render(*parts)

    phase = str(data.get("phase") or "?")
    head = Text()
    head.append(str(data.get("profile") or "?"), style="cli.key")
    head.append("  ")
    head.append(phase, style=_PHASE_STYLE.get(phase, ""))
    elapsed = _duration(data.get("elapsed_s"))
    if elapsed:
        head.append(f"  {elapsed}", style="cli.muted")
    project = str(data.get("project_id") or "").strip()
    if project:
        head.append(f"  [{project}]", style="cli.muted")
    parts = [heading("Live run"), head]

    reason = str(data.get("reason") or "").strip()
    if reason:
        parts.append(muted(f"  reason: {truncate(reason, 100)}"))
    run_id = str(data.get("run_id") or "").strip()
    if run_id:
        parts.append(muted(f"  run: {run_id}"))

    probes = _probe_bits(data)
    if probes:
        parts.append(muted("  probes (last ok / stall budget):"))
        for pid, text, style in probes:
            line = Text()
            line.append(f"    {pid:<22}", style="cli.key")
            line.append(text, style=style)
            parts.append(line)

    caps = data.get("caps")
    would_refuse = caps.get("would_refuse") if isinstance(caps, dict) else None
    if would_refuse:
        parts.append(Text(f"  caps: a relaunch would be refused — {would_refuse}",
                          style="cli.warn"))

    cycles, cap = data.get("fix_cycles_today"), data.get("fix_cap")
    if cycles is not None and cap is not None:
        parts.append(muted(f"  fix cycles today: {cycles}/{cap}"))

    literals = data.get("literals")
    if isinstance(literals, dict) and literals:
        rendered = "  ".join(f"{k}={v}" for k, v in sorted(literals.items()))
        parts.append(muted(f"  literals: {truncate(rendered, 120)}"))

    for hold in _holds(data):
        parts.append(Text(f"  {hold}", style="cli.bad"))
    return render(*parts)


_VERDICTS = {
    "started": ("cli.ok", "started"),
    "stopping": ("cli.warn", "stopping — evidence, then teardown"),
    "resumed": ("cli.ok", "resumed"),
    "paused": ("cli.ok", "fix loop paused"),
    "pausing": ("cli.warn", "fix loop paused; the live run was asked to abort its cycle"),
    "refused": ("cli.bad", "refused"),
    "error": ("cli.bad", "error"),
    "empty": ("cli.muted", "nothing to do"),
}


def render_verdict(payload: Any, verb: str) -> str:
    """A mutation's one-line answer, with the manager's own reason kept intact."""
    data = payload if isinstance(payload, dict) else {}
    status = str(data.get("status") or "").strip() or "?"
    style, label = _VERDICTS.get(status, ("", status))
    line = Text()
    line.append(f"{verb}: ", style="cli.muted")
    line.append(label, style=style)
    detail = str(data.get("reason") or data.get("detail") or "").strip()
    if detail:
        line.append(f" — {truncate(detail, 100)}", style="cli.muted")
    run_id = str(data.get("run_id") or "").strip()
    if run_id:
        line.append(f"  ({run_id})", style="cli.muted")
    return render(line)
