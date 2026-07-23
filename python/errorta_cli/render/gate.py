"""Acceptance/test gate view (``GET /test-runs`` → ``{runs:[...]}``).

Each run is a grounded test-command session: ``passed`` (the overall verdict),
``head``/``sandbox``/``at`` provenance, and ``results[]`` with per-command
``exit_code``/``status``. We surface, top-down:

* the latest verdict — ``PASS 9/12`` / ``FAIL 6/12`` (tally = per-command
  ``exit_code==0``, falling back to the run-level ``passed`` when a run recorded
  no per-command exit codes);
* the failing command ids with their exit codes;
* the pass-count trend across recent runs (``5/12 → 9/12 → 9/12``) so a stuck
  gate reads as stuck.

Field selection only (golden invariant #5): stdout hashes/previews on a result are
never rendered; ``--json`` is the sole raw surface.
"""
from __future__ import annotations

from typing import Any

from rich.text import Text

from . import heading, muted, render, ts

_TREND_RUNS = 6


def _result_ok(result: Any) -> bool:
    """A single command result passed?

    Prefer the grounded ``exit_code`` (``0`` == pass); fall back to the result's
    own ``passed`` flag when the exit code was not recorded (e.g. blocked).
    """
    if not isinstance(result, dict):
        return False
    code = result.get("exit_code")
    if code is not None:
        return code == 0
    return bool(result.get("passed"))


def _counts(run: dict[str, Any]) -> tuple[int, int]:
    """``(passed, total)`` for a run.

    Total is the number of recorded results. Passed counts per-command
    ``exit_code==0`` when any exit code is present; otherwise it collapses to the
    run-level ``passed`` verdict (all-or-nothing) so a run without per-command
    exit codes still reads sensibly.
    """
    results = run.get("results") or []
    total = len(results)
    if any(isinstance(r, dict) and r.get("exit_code") is not None for r in results):
        passed = sum(1 for r in results if _result_ok(r))
    elif total:
        passed = total if run.get("passed") else 0
    else:
        passed = 0
    return passed, total


def _short_sha(value: Any) -> str:
    text = str(value or "").strip()
    return text[:8]


def _meta(run: dict[str, Any]) -> str:
    head = _short_sha(run.get("head"))
    when = ts(run.get("at"))
    sandbox = str(run.get("sandbox") or "").strip()
    bits = []
    if head:
        bits.append(f"@{head}")
    if when:
        bits.append(when)
    if sandbox:
        bits.append(f"[{sandbox}]")
    return "  ".join(bits)


def render_gate(payload: Any, verbosity: Any) -> str:
    runs = (payload or {}).get("runs") or []
    if not runs:
        return render(muted("no gate runs recorded"))

    latest = runs[-1]
    passed, total = _counts(latest)
    ok = bool(latest.get("passed"))
    parts = [heading("Gate status")]

    verdict = Text()
    verdict.append(f"{'PASS' if ok else 'FAIL'} {passed}/{total}",
                   style="cli.ok" if ok else "cli.bad")
    meta = _meta(latest)
    if meta:
        verdict.append(f"  {meta}", style="cli.muted")
    parts.append(verdict)

    failing = [r for r in (latest.get("results") or []) if not _result_ok(r)]
    if failing:
        parts.append(muted("failing commands:"))
        for result in failing:
            cid = str(result.get("command_id") or "?")
            code = result.get("exit_code")
            detail = f"exit {code}" if code is not None else str(result.get("status") or "no-exit")
            line = Text()
            line.append(f"  {cid}  ", style="cli.key")
            line.append(detail, style="cli.bad")
            parts.append(line)

    if len(runs) > 1:
        recent = runs[-_TREND_RUNS:]
        trend = " → ".join(f"{p}/{t}" for p, t in (_counts(r) for r in recent))
        parts.append(muted(f"trend: {trend}"))

    return render(*parts)
