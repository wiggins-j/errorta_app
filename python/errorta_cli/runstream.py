"""Live run streaming + terminal classification (F147 §8.2 "start", §4.4).

A started run has NO push channel — the sidecar writes progress to append-only
ledgers and the client polls (survey §2). So the ``run`` command drives the S2
:class:`~errorta_cli.poller.Poller` over the CLI's OWN sidecar (golden invariant
#6) to synthesize events for the active view, and independently polls
``GET /coding/projects/{id}/run`` each tick to detect the terminal state.

Terminal detection + exit-code classing are pure functions (unit-tested without a
sidecar):

* ``is_terminal`` — the run is done when it is not ``running`` and its state
  ``status`` is a terminal one (``stopped`` / ``failed`` / ``interrupted``). The
  start route sets ``status="running"`` synchronously before returning, so the
  first poll after ``POST /run`` is never a false terminal.
* ``classify_exit`` — a FAILURE-class ``stop_reason`` (or a ``failed`` /
  ``interrupted`` state) → ``EXIT_RUN_FAILED`` (7); a success class
  (``definition_of_done`` / ``checkpoint`` / ``cancelled`` / ``no_actionable_work``)
  or an unknown reason → ``EXIT_OK`` (0). The stop-reason sets are grounded in
  ``autonomy.py:36`` + ``topology.Complete`` (survey §2).
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .errors import EXIT_OK, EXIT_RUN_FAILED
from .poller import Poller, events_for_view
from .render.runctl import render_stream_event

# Run-state ``status`` values that mean "the loop is no longer live" (survey §2:
# stopped=loop returned a LoopResult, failed=worker exception, interrupted=orphan
# reconciled). ``idle`` is pre-start and ``running`` is live — neither terminal.
TERMINAL_STATUSES = frozenset({"stopped", "failed", "interrupted"})

# stop_reasons that are a genuine failure (autonomy.py:36; the FAILURE class).
FAILURE_STOP_REASONS = frozenset({
    "budget_exhausted", "no_progress", "hard_blocker", "member_unhealthy",
    "worker_unproductive", "completion_blocked", "not_converging",
})

# stop_reasons that are a clean finish / benign stop (exit 0).
SUCCESS_STOP_REASONS = frozenset({
    "definition_of_done", "checkpoint", "cancelled", "no_actionable_work",
})

# One-line human gloss per terminal reason (rendered at the end of a stream).
STOP_REASON_GLOSS: dict[str, str] = {
    "definition_of_done": "the team met the definition of done",
    "checkpoint": "stopped at a checkpoint — resume to continue",
    "cancelled": "cancelled by request",
    "no_actionable_work": "no actionable work remained",
    "budget_exhausted": "budget (iterations / model-calls) exhausted",
    "no_progress": "the PM made no progress (idle limit reached)",
    "hard_blocker": "a hard blocker stopped the run",
    "member_unhealthy": "a team member's provider kept failing",
    "worker_unproductive": "a worker produced no usable output",
    "completion_blocked": "completion was refused / blocked",
    "not_converging": "the work stopped converging",
    "interrupted": "the run was interrupted (recoverable — resume it)",
    "failed": "the run failed with an unexpected error",
}


def _state(run_payload: Any) -> dict[str, Any]:
    return (run_payload or {}).get("state") or {}


def is_terminal(run_payload: Any) -> bool:
    """True when the run has reached a terminal state (not running + terminal status)."""
    if not isinstance(run_payload, dict):
        return False
    if run_payload.get("running"):
        return False
    return str(_state(run_payload).get("status") or "") in TERMINAL_STATUSES


def terminal_stop_reason(run_payload: Any) -> str | None:
    """The reason a terminal run stopped (state-derived where there's no stop_reason)."""
    state = _state(run_payload)
    status = str(state.get("status") or "")
    if status == "failed":
        return "failed"
    if status == "interrupted":
        return "interrupted"
    reason = state.get("stop_reason")
    return str(reason) if reason else None


def classify_exit(run_payload: Any) -> int:
    """Map a terminal run to ``EXIT_OK`` (success/benign) or ``EXIT_RUN_FAILED``."""
    state = _state(run_payload)
    status = str(state.get("status") or "")
    if status in ("failed", "interrupted"):
        return EXIT_RUN_FAILED
    if state.get("stop_reason") in FAILURE_STOP_REASONS:
        return EXIT_RUN_FAILED
    return EXIT_OK


def gloss(reason: str | None) -> str:
    """Human gloss for a terminal reason (falls back to the raw reason)."""
    if not reason:
        return "run finished"
    return STOP_REASON_GLOSS.get(reason, reason)


def _run_path(project_id: str) -> str:
    return f"/coding/projects/{project_id}/run"


def _visible_channels(verbosity: Any) -> set[str]:
    """The channels the current verbosity dial would stream (for event polling)."""
    from .verbosity import CHANNELS

    if verbosity is None:
        return set(CHANNELS)
    return {ch for ch in CHANNELS if verbosity.should_emit(ch)}


def block_until_terminal(
    client: Any,
    project_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 2.5,
    max_ticks: int | None = None,
) -> dict[str, Any]:
    """Poll ONLY ``GET /run`` until terminal — the ``--json`` block-to-done path.

    No live view (a machine consumer wants the terminal JSON, not a stream). Only
    ever touches the CLI's own sidecar (invariant #6).
    """
    ticks = 0
    last: dict[str, Any] = {}
    while True:
        last = client.get_json(_run_path(project_id)) or {}
        if is_terminal(last):
            return last
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return last
        sleep(interval)


def stream_run(
    client: Any,
    ctx: Any,
    *,
    poller: Poller | None = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
    max_ticks: int | None = None,
) -> dict[str, Any]:
    """Drive the live view until the run is terminal; return the final ``GET /run``.

    Each tick: synthesize new ledger events (via the S2 poller, scoped to the
    channels the verbosity dial shows) and render them one line at a time, then
    poll ``GET /run`` for the terminal state. Raises nothing on its own —
    ``KeyboardInterrupt`` (detach) is handled by the caller. Only touches the
    CLI's own sidecar (invariant #6, inherited from the injected client/poller).
    """
    project_id = ctx.project_id
    poller = poller or Poller(
        client, project_id, verbosity=ctx.verbosity,
        interval_override=ctx.poll_interval,
    )
    channels = _visible_channels(ctx.verbosity)
    ticks = 0
    last: dict[str, Any] = {}
    while True:
        for event in events_for_view(poller.poll_once(channels=channels), ctx.verbosity):
            line = render_stream_event(event)
            if line:
                emit(line)
        last = client.get_json(_run_path(project_id)) or {}
        if is_terminal(last):
            return last
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return last
        sleep(poller.base_interval)
