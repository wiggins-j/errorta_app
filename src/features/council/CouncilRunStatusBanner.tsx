// F031 Phase 2 — Run status banner. Unknown backend status renders as
// "Unknown state" with a copyable detail block (invariant 4).
import type { CouncilRunStatus } from "./types";

interface Props {
  status: CouncilRunStatus;
}

const LABELS: Record<string, string> = {
  idle: "Idle",
  validating: "Validating",
  ready: "Ready",
  submitting: "Submitting",
  running: "Running",
  paused: "Paused",
  awaiting_decision: "Awaiting decision",
  cancelling: "Cancelling",
  finalizing: "Finalizing",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  unknown: "Unknown state",
};

export default function CouncilRunStatusBanner({ status }: Props) {
  const isUnknown = status.state === "unknown";
  const isError = status.state === "failed";
  const cls = isUnknown
    ? "council-status-banner unknown"
    : isError
      ? "council-status-banner error"
      : "council-status-banner";
  return (
    <div className={cls} role="status" aria-live="polite">
      {LABELS[status.state] ?? "Unknown state"}
      {status.terminalReason ? ` · ${status.terminalReason}` : ""}
      {isUnknown && (
        <pre style={{ fontSize: "0.72rem", margin: "0.25rem 0 0" }}>
          {JSON.stringify({ backendStatus: status.backendStatus }, null, 2)}
        </pre>
      )}
    </div>
  );
}
