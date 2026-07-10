// F031 Phase 5 Task 4 — Run pause / resume / cancel controls.
import type { CouncilRunStatus } from "./types";

interface Props {
  status: CouncilRunStatus | null;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
}

const TERMINAL = new Set(["done", "failed", "cancelled"]);

export default function CouncilRunControls({
  status,
  onPause,
  onResume,
  onCancel,
}: Props) {
  if (!status) return null;
  const state = status.state;
  const terminal = TERMINAL.has(state);
  // F031-09: /resume is valid from BOTH ``paused`` and
  // ``awaiting_decision`` — backend treats the latter as an implicit
  // ``continue_local_only`` decision. Without this widening the UI
  // strands ask-paused runs (QA P2 review finding).
  const canResume = state === "paused" || state === "awaiting_decision";
  return (
    <div className="council-run-controls" role="toolbar" aria-label="Run controls">
      <button
        type="button"
        onClick={onPause}
        disabled={state !== "running"}
        aria-label="Pause run"
      >
        Pause
      </button>
      <button
        type="button"
        onClick={onResume}
        disabled={!canResume}
        aria-label="Resume run"
      >
        Resume
      </button>
      <button
        type="button"
        onClick={onCancel}
        disabled={terminal}
        aria-label="Cancel run"
      >
        Cancel
      </button>
    </div>
  );
}
