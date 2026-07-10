import { describe, expect, it } from "vitest";

import { deriveRunPhase } from "./runPhase";
import type { RunStatus } from "../../lib/api/coding";

function status(over: Partial<RunStatus> = {}): RunStatus {
  return {
    running: false,
    result: null,
    recoverable: false,
    canResume: false,
    ...over,
  };
}

describe("deriveRunPhase (F121 Part A)", () => {
  it("returns 'starting' on optimistic Start before the poll confirms running", () => {
    expect(
      deriveRunPhase({ intent: "starting", running: false, runStatus: status() }),
    ).toBe("starting");
  });

  it("flips 'starting' -> 'working' once the backend reports running", () => {
    expect(
      deriveRunPhase({
        intent: "starting",
        running: true,
        runStatus: status({ running: true, state: { status: "running" } }),
      }),
    ).toBe("working");
  });

  it("falls out of 'starting' to a terminal/idle state on timeout (never hangs)", () => {
    // The run never reached running and the bounded timeout elapsed.
    expect(
      deriveRunPhase({
        intent: "starting",
        running: false,
        runStatus: status(),
        startTimedOut: true,
      }),
    ).toBe("idle");
  });

  it("a terminal status supersedes a lingering 'starting' intent", () => {
    // The worker can fail or stop before a poll ever reports `running`; don't
    // wait for the 12s start timeout when the backend already has a terminal
    // state.
    expect(
      deriveRunPhase({
        intent: "starting",
        running: false,
        runStatus: status({
          state: { status: "failed" },
          result: { stop_reason: "member_unhealthy" },
        }),
      }),
    ).toBe("stopped");
  });

  it("'starting' wins over a STALE prior-run terminal during preflight", () => {
    // The bug: clicking Start on a project whose last run ended terminally
    // (stopped/interrupted/failed). While the new start is in preflight the
    // backend still reports the PRIOR run's terminal state — same started_at as
    // when we clicked — so the optimistic "Starting…" must win for immediate
    // feedback instead of sitting on the old "stopped" for 5-10s.
    const startedAt = "2026-06-27T00:00:00Z";
    expect(
      deriveRunPhase({
        intent: "starting",
        running: false,
        startBaseline: startedAt,
        runStatus: status({
          state: { status: "stopped", started_at: startedAt },
          result: { stop_reason: "member_unhealthy" },
        }),
      }),
    ).toBe("starting");
  });

  it("a FRESH terminal (new run died, started_at changed) supersedes 'starting'", () => {
    // Once the NEW run actually starts and then dies, its started_at differs from
    // the captured baseline — that terminal is authoritative, so don't hang on
    // "Starting…".
    expect(
      deriveRunPhase({
        intent: "starting",
        running: false,
        startBaseline: "2026-06-27T00:00:00Z",
        runStatus: status({
          state: { status: "failed", started_at: "2026-06-27T00:05:00Z" },
          result: { stop_reason: "member_unhealthy" },
        }),
      }),
    ).toBe("stopped");
  });

  it("returns 'stopping' on optimistic Stop", () => {
    expect(
      deriveRunPhase({
        intent: "stopping",
        running: true,
        runStatus: status({ running: true, state: { status: "running" } }),
      }),
    ).toBe("stopping");
  });

  it("derives 'stopping' from cancel_requested while running (survives reload)", () => {
    // No optimistic intent (e.g. after a reload) — the sticky backend flag alone
    // must keep the phase at 'stopping' so a draining run doesn't look frozen.
    expect(
      deriveRunPhase({
        intent: "none",
        running: true,
        runStatus: status({
          running: true,
          state: { status: "running", cancel_requested: true },
        }),
      }),
    ).toBe("stopping");
  });

  it("returns 'stopped' for a terminal run", () => {
    expect(
      deriveRunPhase({
        intent: "none",
        running: false,
        runStatus: status({ state: { status: "stopped" }, result: { stop_reason: "cancelled" } }),
      }),
    ).toBe("stopped");
  });

  it("returns 'idle' when nothing is running or requested", () => {
    expect(deriveRunPhase({ intent: "none", running: false, runStatus: status() })).toBe("idle");
  });

  it("a terminal status supersedes a lingering 'stopping' intent (no stuck Stopping…)", () => {
    // Regression: the user clicked Stop (intent stays "stopping") but the run
    // then ended on its own (status "stopped", e.g. budget_exhausted). The phase
    // must resolve to "stopped" — it used to hang on "Stopping…" forever.
    expect(
      deriveRunPhase({
        intent: "stopping",
        running: false,
        runStatus: status({
          state: { status: "stopped", cancel_requested: true },
          result: { stop_reason: "budget_exhausted" },
        }),
      }),
    ).toBe("stopped");
    // Same for an interrupted/failed terminal.
    expect(
      deriveRunPhase({
        intent: "stopping",
        running: false,
        runStatus: status({ state: { status: "failed" } }),
      }),
    ).toBe("stopped");
  });
});
