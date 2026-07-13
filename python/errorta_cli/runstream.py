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
* ``classify_exit`` — classification is an **allowlist that fails closed** so the
  exit-code contract can't silently drift: only the success allowlist
  (``definition_of_done`` / ``checkpoint`` / ``cancelled`` / ``no_actionable_work``)
  → ``EXIT_OK`` (0). A FAILURE-class ``stop_reason``, a ``failed`` /
  ``interrupted`` state, **or an unknown/未-triaged terminal reason** →
  ``EXIT_RUN_FAILED`` (7). A future engine ``stop_reason`` the CLI hasn't
  classified therefore reads as CI failure (non-zero), never a false success.
  The stop-reason sets are grounded in ``autonomy.py:36`` (survey §2) and locked
  against drift by ``test_every_engine_stop_reason_is_triaged``.

Live polling is **blip-tolerant**: a transient ``GET /run`` failure mid-stream
must not abort a still-live run with exit 9, so both loops tolerate up to
``POLL_ERROR_TOLERANCE`` *consecutive* poll errors (small backoff between them; a
successful poll resets the counter) before giving up with
:class:`RunStreamDetached` — which the ``run`` command surfaces as a graceful
"detached, run continues" (exit 0), not a hard sidecar-unreachable failure.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .errors import EXIT_OK, EXIT_RUN_FAILED, CliError
from .poller import Poller, events_for_view
from .render.runctl import render_stream_event

# Consecutive ``GET /run`` poll failures tolerated during a live stream before we
# give up on the view (a single transient blip must not kill a live run). A
# successful poll resets the counter; exceeding it raises :class:`RunStreamDetached`.
POLL_ERROR_TOLERANCE = 3
POLL_ERROR_BACKOFF = 1.0  # seconds between tolerated retries


class RunStreamDetached(Exception):
    """The stream gave up polling (too many consecutive errors) — NOT a failure.

    The run itself is still running in the background; the client just lost its
    view of it. Carries the last-seen ``GET /run`` payload (may be empty). The
    ``run`` command maps this to a graceful detach (exit 0), never exit 9.
    """

    def __init__(self, last: Any = None) -> None:
        super().__init__("run stream detached after repeated poll failures")
        self.last = last or {}

# Run-state ``status`` values that mean "the loop is no longer live" (survey §2:
# stopped=loop returned a LoopResult, failed=worker exception, interrupted=orphan
# reconciled). ``idle`` is pre-start and ``running`` is live — neither terminal.
TERMINAL_STATUSES = frozenset({"stopped", "failed", "interrupted"})

# stop_reasons that are a genuine failure (autonomy.py:36; the FAILURE class).
FAILURE_STOP_REASONS = frozenset({
    "budget_exhausted", "no_progress", "hard_blocker", "member_unhealthy",
    "worker_unproductive", "completion_blocked", "not_converging",
    "delivery_review_stalled",
})

# stop_reasons that are a clean finish / benign stop (exit 0).
SUCCESS_STOP_REASONS = frozenset({
    "definition_of_done", "checkpoint", "cancelled", "no_actionable_work",
})

# One-line human gloss per terminal reason (rendered at the end of a stream).
STOP_REASON_GLOSS: dict[str, str] = {
    "definition_of_done": "the team met the definition of done",
    "checkpoint": "stopped at a checkpoint — continue it with: errorta continue",
    "cancelled": "cancelled by request",
    "no_actionable_work": "no actionable work remained",
    "budget_exhausted": "budget (iterations / model-calls) exhausted",
    "no_progress": "the PM made no progress (idle limit reached)",
    "hard_blocker": "a hard blocker stopped the run",
    "member_unhealthy": "a team member's provider kept failing",
    "worker_unproductive": "a worker produced no usable output",
    "completion_blocked": "completion was refused / blocked",
    "not_converging": "the work stopped converging",
    "delivery_review_stalled": ("delivery review kept rejecting the integrated "
                                "result — stopped instead of burning budget"),
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
    """Map a terminal run to ``EXIT_OK`` (success/benign) or ``EXIT_RUN_FAILED``.

    Allowlist + fail-closed: a ``failed`` / ``interrupted`` status is always a
    failure; otherwise ONLY a ``stop_reason`` in :data:`SUCCESS_STOP_REASONS` is
    ``EXIT_OK``. A failure-class reason, an unknown/未-triaged reason, or a
    missing reason all classify as :data:`EXIT_RUN_FAILED` — for a headless CI
    tool, an unrecognized terminal reason defaulting to "success" is the wrong
    direction, so we fail closed.
    """
    state = _state(run_payload)
    status = str(state.get("status") or "")
    if status in ("failed", "interrupted"):
        return EXIT_RUN_FAILED
    if state.get("stop_reason") in SUCCESS_STOP_REASONS:
        return EXIT_OK
    return EXIT_RUN_FAILED


def gloss(reason: str | None) -> str:
    """Human gloss for a terminal reason (falls back to the raw reason)."""
    if not reason:
        return "run finished"
    return STOP_REASON_GLOSS.get(reason, reason)


def _run_path(project_id: str) -> str:
    return f"/coding/projects/{project_id}/run"


def _poll_retry_note(errors: int) -> str:
    return f"(poll failed — retrying {errors}/{POLL_ERROR_TOLERANCE})"


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
    ever touches the CLI's own sidecar (invariant #6). Tolerates transient poll
    errors (see :data:`POLL_ERROR_TOLERANCE`); raises :class:`RunStreamDetached`
    only after too many *consecutive* failures.
    """
    ticks = 0
    errors = 0
    last: dict[str, Any] = {}
    while True:
        try:
            last = client.get_json(_run_path(project_id)) or {}
        except CliError as exc:
            errors += 1
            if errors > POLL_ERROR_TOLERANCE:
                raise RunStreamDetached(last) from exc
            sleep(POLL_ERROR_BACKOFF)
            continue
        errors = 0
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
    errors = 0
    last: dict[str, Any] = {}
    while True:
        # The whole tick (ledger event synthesis + the terminal poll) is
        # blip-tolerant: a transient sidecar error retries the tick with backoff
        # rather than aborting a live run. A clean tick resets the error budget.
        try:
            for event in events_for_view(poller.poll_once(channels=channels), ctx.verbosity):
                line = render_stream_event(event)
                if line:
                    emit(line)
            last = client.get_json(_run_path(project_id)) or {}
        except CliError as exc:
            errors += 1
            if errors > POLL_ERROR_TOLERANCE:
                raise RunStreamDetached(last) from exc
            emit(_poll_retry_note(errors))
            sleep(POLL_ERROR_BACKOFF)
            continue
        errors = 0
        if is_terminal(last):
            return last
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return last
        sleep(poller.base_interval)
