// F005 — top-of-corpus "Watching: <path>" badge with pause/change/stop.
import type { DeletionPolicy, WatchStatus } from "./types";
import { WatchStatusIndicator } from "./WatchStatusIndicator";

interface Props {
  status: WatchStatus;
  onPause: () => void;
  onResume: () => void;
  onChange: () => void;
  onStop: () => void;
  onSetDeletionPolicy: (p: DeletionPolicy) => void;
  onForceRescan?: (corpus: string) => void;
}

export function FolderWatchBadge({
  status,
  onPause,
  onResume,
  onChange,
  onStop,
  onSetDeletionPolicy,
  onForceRescan,
}: Props) {
  if (!status.watching) return null;
  return (
    <div className="folder-watch-badge">
      <div className="folder-watch-title-row">
        <strong>Watching:</strong>
        <code>{status.watched_path}</code>
        <span>
          · {status.file_count ?? 0} files
        </span>
      </div>
      <div className="folder-watch-health">
        <WatchStatusIndicator status={status} onForceRescan={onForceRescan} />
      </div>
      <div className="folder-watch-actions">
        {status.paused ? (
          <button type="button" onClick={onResume}>
            Resume
          </button>
        ) : (
          <button type="button" onClick={onPause}>
            Pause
          </button>
        )}
        <button type="button" onClick={onChange}>
          Change folder
        </button>
        <button type="button" onClick={onStop}>
          Stop watching
        </button>
        <label>
          On delete:&nbsp;
          <select
            value={status.deletion_policy ?? "remove"}
            onChange={(e) => onSetDeletionPolicy(e.target.value as DeletionPolicy)}
          >
            <option value="remove">Remove from corpus</option>
            <option value="mark_missing">Keep, mark source missing</option>
          </select>
        </label>
      </div>
    </div>
  );
}
