"""One live run dashboard (Spec 06) — a compact multi-route panel.

Client-side composition (no new route): ``commands/watch.py`` polls ``/run``,
``/usage-summary``, ``/test-runs``, ``/team-log`` (+ ``/turns?limit=``) and hands
the merged snapshot here. We draw ONE compact panel so a working-but-quiet run
doesn't read as hung::

    run: running  turn 61  caps: iter 61/200  calls 120/∞  [converging]
    tokens: 1,331,926 (in 900,000 / out 431,926)
    gate: 9/12  (trend 7/12 → 9/12)
    members: pm-1 plan_posted · dev-1 pr_opened
    last: 16:03 dev-1 pr_opened — opened PR for t-3

Field selection only (golden invariant #5): we surface status/counter/cap
*numbers*, token totals, gate PASS-counts, and team-log role/member/kind/message
— never a raw dict, a turn prompt/response, or any ``_secret``. ``--json`` is the
sole raw surface.

Convergence indicator (display-only) is derived from THIS snapshot: ``done`` on a
terminal ``stop_reason``; ``converging`` when the latest gate pass-count rose over
the prior run; ``stalled`` when the last two gate runs are unchanged; else
``running`` / ``idle``. The poll harness re-dispatches a fresh compose each tick
and does not thread the prior tick's counters through, so the indicator reads the
in-payload gate trend rather than a cross-tick counter delta (Spec 06 blessed
this simpler-but-honest derivation over faking a cross-tick signal).
"""
from __future__ import annotations

from typing import Any

from rich.text import Text

from . import render, truncate, ts
from .gate import _TREND_RUNS, _counts  # reuse the gate pass-count logic

_MEMBER_TAIL = 3

_INDICATOR_STYLE = {
    "done": "cli.ok",
    "converging": "cli.ok",
    "stalled": "cli.bad",
    "running": "cli.warn",
    "idle": "cli.muted",
}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fmt_cap(value: Any) -> str:
    """A cap value for the panel: ``None`` reads as unlimited (``∞``)."""
    return "∞" if value is None else str(value)


def _indicator(state: dict[str, Any], gate_passes: list[int], running: bool) -> str:
    """Convergence signal for the current snapshot (see module docstring)."""
    if state.get("stop_reason"):
        return "done"
    if len(gate_passes) >= 2:
        if gate_passes[-1] > gate_passes[-2]:
            return "converging"
        if gate_passes[-1] == gate_passes[-2]:
            return "stalled"
    return "running" if running else "idle"


def _members_line(entries: list[Any], turns: list[Any]) -> str:
    """Recent per-member activity, newest last.

    Prefer the team-log tail (each entry carries ``member``/``kind``); fall back to
    ``/turns`` (``member``/``role`` + ``outcome``) when the log has no members.
    """
    seen: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        member = str(entry.get("member") or "").strip()
        if member:
            seen[member] = str(entry.get("kind") or "").strip()
    if seen:
        tail = list(seen.items())[-_MEMBER_TAIL:]
        return " · ".join(f"{m} {k}".strip() for m, k in tail)

    bits: list[str] = []
    for turn in turns[-_MEMBER_TAIL:]:
        if not isinstance(turn, dict):
            continue
        who = str(turn.get("member") or turn.get("role") or "?").strip()
        what = str(turn.get("outcome") or turn.get("route_id") or "").strip()
        bits.append(f"{who} {what}".strip())
    return " · ".join(b for b in bits if b) or "(none)"


def _last_event(entries: list[Any]) -> str:
    """The most recent team-log entry, compacted (never a raw dump)."""
    if not entries or not isinstance(entries[-1], dict):
        return "(no events)"
    entry = entries[-1]
    when = ts(entry.get("at"))
    who = str(entry.get("member") or entry.get("role") or "").strip()
    kind = str(entry.get("kind") or "").strip()
    head = " ".join(b for b in (when, who, kind) if b)
    message = truncate(entry.get("message"), 80)
    if message:
        return f"{head} — {message}" if head else message
    return head or "(no events)"


def render_watch(payload: Any, verbosity: Any) -> str:
    payload = payload or {}
    run = payload.get("run") or {}
    state = run.get("state") or {}
    counters = state.get("counters") or {}
    running = bool(run.get("running"))
    status = "running" if running else str(state.get("status") or "idle")

    runs = (payload.get("test_runs") or {}).get("runs") or []
    gate_counts = [_counts(r) for r in runs if isinstance(r, dict)]
    gate_passes = [p for p, _ in gate_counts]
    indicator = _indicator(state, gate_passes, running)

    # line 1 — run status + turn + caps + convergence indicator
    line1 = Text()
    line1.append("run: ", style="cli.muted")
    line1.append(status, style="cli.key")
    iterations = counters.get("iterations")
    if iterations is not None:
        line1.append(f"  turn {iterations}")
    caps = run.get("caps") or {}
    if caps:
        seg = f"  caps: iter {_int(iterations)}/{_fmt_cap(caps.get('max_iterations'))}"
        if counters.get("model_calls") is not None or caps.get("max_model_calls") is not None:
            seg += (
                f"  calls {_int(counters.get('model_calls'))}"
                f"/{_fmt_cap(caps.get('max_model_calls'))}"
            )
        line1.append(seg, style="cli.muted")
    line1.append("  [")
    line1.append(indicator, style=_INDICATOR_STYLE.get(indicator, "white"))
    line1.append("]")
    stop_reason = state.get("stop_reason")
    if stop_reason:
        line1.append(f" {stop_reason}", style="cli.muted")

    # line 2 — token rollup total (in / out)
    total = ((payload.get("usage") or {}).get("usage") or {}).get("total") or {}
    tin, tout = _int(total.get("input")), _int(total.get("output"))
    line2 = Text()
    line2.append("tokens: ", style="cli.muted")
    line2.append(f"{tin + tout:,}")
    line2.append(f" (in {tin:,} / out {tout:,})", style="cli.muted")

    # line 3 — gate pass-count + trend (reused gate logic)
    line3 = Text()
    line3.append("gate: ", style="cli.muted")
    if gate_counts:
        passed, total_cmds = gate_counts[-1]
        line3.append(f"{passed}/{total_cmds}")
        if len(gate_counts) > 1:
            trend = " → ".join(f"{p}/{t}" for p, t in gate_counts[-_TREND_RUNS:])
            line3.append(f"  (trend {trend})", style="cli.muted")
    else:
        line3.append("no runs", style="cli.muted")

    # lines 4 + 5 — member activity + most recent event
    entries = (payload.get("team_log") or {}).get("entries") or []
    turns = (payload.get("turns") or {}).get("turns") or []
    line4 = Text()
    line4.append("members: ", style="cli.muted")
    line4.append(_members_line(entries, turns))
    line5 = Text()
    line5.append("last: ", style="cli.muted")
    line5.append(_last_event(entries))

    return render(line1, line2, line3, line4, line5)
