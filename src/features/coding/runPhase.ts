// F121 — derive the run-control phase from (a) optimistic local intent and
// (b) the polled backend run-state. Pure so it can be unit-tested without React.
//
// The phases the run-control region renders:
//   - "starting": the user clicked Start; no poll has confirmed `running` yet.
//   - "working":  the backend reports status==="running".
//   - "stopping": the user clicked Stop, OR the backend says cancel was
//                 requested while still running (survives reload/poll).
//   - "stopped":  terminal — show the stop_reason.
//   - "idle":     nothing running, nothing requested.
//
// Priority is deliberate: terminal > stopping > starting > working > idle. Once
// the backend reports a terminal state, optimistic local intent must stop
// winning or the controls can hang on "Starting…" / "Stopping…".

import {
  runCancelRequested,
  runStateStatus,
  type RunStatus,
} from "../../lib/api/coding";

export type RunIntent = "none" | "starting" | "stopping";

export type RunPhase = "idle" | "starting" | "working" | "stopping" | "stopped";

export interface RunPhaseInput {
  /** Optimistic local intent set the instant the user clicks Start/Stop. */
  intent: RunIntent;
  /** Whether the backend currently reports a live run (running && thread alive). */
  running: boolean;
  /** The polled run-state (carries status + cancel_requested + result). */
  runStatus: RunStatus | null | undefined;
  /**
   * F121 Part A: when the optimistic "starting" intent has timed out without
   * the run ever reaching `running` (the worker died on spawn, or the start was
   * refused), the caller flips this true so the phase resolves away from
   * "starting" into an honest terminal/error state instead of hanging.
   */
  startTimedOut?: boolean;
  /**
   * The run's `started_at` captured the instant the user clicked Start. Lets a
   * STALE terminal status left by the *previous* run be told apart from *this*
   * start attempt failing: while the new start is in preflight the backend still
   * reports the prior run's terminal state with the SAME `started_at`, so the
   * optimistic "starting" must keep winning (immediate feedback) instead of being
   * suppressed by that stale terminal. Once a NEW run starts, `started_at`
   * changes and the terminal/working state becomes authoritative. `null`/absent
   * when there was no prior run, or for callers that don't track it.
   */
  startBaseline?: string | null;
}

/** The run's `started_at` off the snake_case run-state passthrough, or `null`. */
function runStartedAt(s: RunStatus | null | undefined): string | null {
  const v = s?.state?.["started_at"];
  return typeof v === "string" && v ? v : null;
}

export function deriveRunPhase(input: RunPhaseInput): RunPhase {
  const { intent, running, runStatus, startTimedOut, startBaseline } = input;
  const status = runStateStatus(runStatus);
  const cancelRequested = runCancelRequested(runStatus);
  const terminal =
    status === "stopped" || status === "interrupted" || status === "failed";

  // A terminal status that is LEFT OVER from the previous run: the user just
  // clicked Start, but the backend is still in preflight and hasn't started the
  // new run yet, so it keeps reporting the prior run's terminal state with the
  // unchanged `started_at`. This is the case where the optimistic "starting" must
  // win so the click gets immediate feedback (gray + "Starting…") instead of
  // sitting on the old "stopped" state for the seconds preflight takes.
  const stalePriorTerminal =
    terminal &&
    startBaseline != null &&
    runStartedAt(runStatus) === startBaseline;

  // Stopping wins WHILE the run is still draining (NOT yet terminal): an explicit
  // Stop click, or a backend-recorded cancel with the run still running (survives
  // reloads/polls). Once the backend reports a terminal status, the terminal
  // state below supersedes the optimistic "stopping" intent — otherwise
  // "Stopping…" hangs forever after a run actually ends (e.g. it stopped on its
  // own with stop_reason="budget_exhausted" while the user's Stop click left the
  // intent set).
  if (
    !terminal &&
    (intent === "stopping" || (cancelRequested && (running || status === "running")))
  ) {
    return "stopping";
  }

  // Optimistic Start: hold "starting" until the run reaches running, OR the
  // bounded timeout elapses, OR the backend reports a FRESH terminal (this start
  // attempt's own run died — `started_at` changed). A STALE prior-run terminal
  // does NOT supersede: the user just clicked and the new run is still spinning
  // up, so they get immediate feedback instead of the old "stopped" lingering.
  if (
    intent === "starting" &&
    !running &&
    status !== "running" &&
    !startTimedOut &&
    (!terminal || stalePriorTerminal)
  ) {
    return "starting";
  }

  // Terminal states supersede optimistic intent. This covers Stop-clicks that
  // drain to stopped, and Start-clicks where THIS run's worker fails/stops before
  // any poll ever reported `running` (a fresh terminal, handled above).
  if (terminal) {
    return "stopped";
  }

  if (running || status === "running") return "working";

  return "idle";
}
