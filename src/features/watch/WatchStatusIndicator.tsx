// F005 — health pip for the per-corpus watcher.
import type { WatchStatus } from "./types";

interface Props {
  status: WatchStatus | null;
  onForceRescan?: (corpus: string) => void;
}

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const diffSec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (diffSec < 5) return "just now";
  if (diffSec < 60) return `${diffSec} seconds ago`;
  const m = Math.round(diffSec / 60);
  if (m < 60) return `${m} minute${m === 1 ? "" : "s"} ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} hour${h === 1 ? "" : "s"} ago`;
  const d = Math.round(h / 24);
  return `${d} day${d === 1 ? "" : "s"} ago`;
}

export function WatchStatusIndicator({ status, onForceRescan }: Props) {
  if (!status || !status.watching) {
    return (
      <span style={{ fontSize: 12, color: "var(--muted, #666)" }}>
        Not watching
      </span>
    );
  }
  let color = "#4caf50";
  let label = "Healthy";
  if (status.paused) {
    color = "#b8860b";
    label = "Paused";
  } else if (!status.alive) {
    color = "#b00020";
    label = "Stopped";
  } else if (status.last_scan_ok === false) {
    color = "#b00020";
    label = "Scan failed";
  }

  const stale = status.stale === true;
  const ageSec = Math.round(status.heartbeat_age_seconds ?? 0);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12 }}>
        <span
          aria-hidden
          style={{
            display: "inline-block",
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: color,
          }}
        />
        <span>{label}</span>
        {status.last_scan_at ? (
          <span
            style={{ color: "var(--muted, #666)" }}
            title={status.last_scan_at}
          >
            · last scan {relativeTime(status.last_scan_at)}
          </span>
        ) : null}
        {status.last_error ? (
          <span style={{ color: "#b00020" }}>· {status.last_error}</span>
        ) : null}
      </span>
      {stale ? (
        <div
          role="alert"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 8px",
            background: "rgba(176, 0, 32, 0.08)",
            border: "1px solid #b00020",
            borderRadius: 4,
            color: "#b00020",
            fontSize: 12,
          }}
        >
          <span>Watcher stale ({ageSec}s since last heartbeat)</span>
          {onForceRescan ? (
            <button
              type="button"
              onClick={() => onForceRescan(status.corpus)}
              style={{ fontSize: 12 }}
            >
              Force rescan
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
