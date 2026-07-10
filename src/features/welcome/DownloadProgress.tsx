// F007 — progress bar for the welcome-corpus download + ingest flow.
import type { WelcomeStatus } from "./types";

interface Props {
  status: WelcomeStatus | null;
}

function phaseLabel(phase: WelcomeStatus["phase"]): string {
  switch (phase) {
    case "downloading":
      return "Downloading…";
    case "verifying":
      return "Verifying SHA-256…";
    case "extracting":
      return "Extracting…";
    case "ingesting":
      return "Ingesting into corpus…";
    case "done":
      return "Done.";
    case "error":
      return "Error.";
    case "idle":
    default:
      return "Idle.";
  }
}

export default function DownloadProgress({ status }: Props) {
  if (!status) return null;
  const pct = Math.max(0, Math.min(100, Math.round(status.progress * 100)));
  const eta =
    status.eta_seconds != null && Number.isFinite(status.eta_seconds)
      ? `${Math.ceil(status.eta_seconds)}s left`
      : null;
  return (
    <div className="welcome-progress" aria-live="polite">
      <div className="welcome-progress-label">
        <span>{phaseLabel(status.phase)}</span>
        <span>{pct}%</span>
      </div>
      <div className="welcome-progress-bar">
        <div
          className="welcome-progress-fill"
          style={{ width: `${pct}%` }}
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct}
        />
      </div>
      <div className="welcome-progress-meta">
        {status.bytes_total
          ? `${status.bytes_downloaded} / ${status.bytes_total} bytes`
          : `${status.bytes_downloaded} bytes`}
        {eta ? ` · ${eta}` : null}
      </div>
      {status.error ? (
        <p className="welcome-progress-error">{status.error}</p>
      ) : null}
    </div>
  );
}
