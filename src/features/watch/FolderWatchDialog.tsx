// F005 — pre-ingest dialog: shows scan summary, type filter, cloud-sync
// warning, and confirms to start watching.
import { useEffect, useState } from "react";
import * as watchApi from "../../lib/api/watch";
import type { DeletionPolicy, PathCheck } from "./types";
import { CloudSyncWarning } from "./CloudSyncWarning";

interface Props {
  corpus: string;
  path: string;
  mode?: "start" | "change";
  onCancel: () => void;
  onStarted: () => void;
  onPickDifferent: () => void;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function estimateMinutes(bytes: number): number {
  // Rough: 6 MB/min through the F004 extraction pipeline. Tuned later.
  return Math.max(1, Math.round(bytes / (6 * 1024 * 1024)));
}

export function FolderWatchDialog({
  corpus,
  path,
  mode = "start",
  onCancel,
  onStarted,
  onPickDifferent,
}: Props) {
  const [check, setCheck] = useState<PathCheck | null>(null);
  const [typeFilter, setTypeFilter] = useState<string[]>([]);
  const [deletionPolicy, setDeletionPolicy] = useState<DeletionPolicy>("remove");
  const [cloudAck, setCloudAck] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const c = await watchApi.checkPath(path, typeFilter);
        if (!cancelled) setCheck(c);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [path, typeFilter]);

  function toggleType(ext: string) {
    setTypeFilter((prev) =>
      prev.includes(ext) ? prev.filter((e) => e !== ext) : [...prev, ext],
    );
  }

  async function startWatching() {
    setStarting(true);
    setError(null);
    try {
      if (mode === "change") {
        // Watcher is already running for this corpus; use change-path so the
        // backend re-points the existing watcher instead of returning 409.
        await watchApi.changePath(corpus, path);
      } else {
        await watchApi.start({
          corpus,
          watched_path: path,
          deletion_policy: deletionPolicy,
          type_filter: typeFilter,
        });
      }
      onStarted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  }

  const needCloudAck = !!check?.cloud_sync_provider && !cloudAck;

  return (
    <div
      role="dialog"
      aria-label="Confirm folder watch"
      style={{
        padding: 16,
        border: "1px solid var(--border, #e0e0e0)",
        borderRadius: 8,
        background: "var(--panel, #fff)",
        margin: "8px 0",
      }}
    >
      <h2 style={{ marginTop: 0 }}>Watch folder</h2>
      <p style={{ margin: "0 0 8px" }}>
        <code>{path}</code>
      </p>

      {check === null && !error ? <p>Scanning folder…</p> : null}

      {check && check.exists ? (
        <>
          <p style={{ margin: "8px 0" }}>
            <strong>Found {check.file_count} supported files</strong>{" "}
            ({formatBytes(check.total_bytes)})<br />
            <span style={{ color: "var(--muted, #666)", fontSize: 13 }}>
              Estimated ingestion time: ~{estimateMinutes(check.total_bytes)} min
            </span>
          </p>

          <fieldset style={{ margin: "12px 0", padding: 8 }}>
            <legend>Filter by type (optional)</legend>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {check.supported_extensions.map((ext) => {
                const clean = ext.replace(/^\./, "");
                const on = typeFilter.includes(clean);
                return (
                  <label key={ext} style={{ fontSize: 13 }}>
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() => toggleType(clean)}
                    />
                    &nbsp;{ext}
                  </label>
                );
              })}
            </div>
          </fieldset>

          <fieldset style={{ margin: "12px 0", padding: 8 }}>
            <legend>When a file disappears from the folder</legend>
            <label style={{ display: "block", fontSize: 13 }}>
              <input
                type="radio"
                name="deletion-policy"
                checked={deletionPolicy === "remove"}
                onChange={() => setDeletionPolicy("remove")}
              />
              &nbsp;Remove from corpus (default)
            </label>
            <label style={{ display: "block", fontSize: 13 }}>
              <input
                type="radio"
                name="deletion-policy"
                checked={deletionPolicy === "mark_missing"}
                onChange={() => setDeletionPolicy("mark_missing")}
              />
              &nbsp;Keep in corpus, mark "source missing"
            </label>
          </fieldset>
        </>
      ) : null}

      {check && !check.exists ? (
        <p style={{ color: "#b00020" }}>That folder doesn't exist.</p>
      ) : null}

      {check?.cloud_sync_provider ? (
        <CloudSyncWarning
          provider={check.cloud_sync_provider}
          path={path}
          onContinue={() => setCloudAck(true)}
          onPickDifferent={onPickDifferent}
        />
      ) : null}

      {error ? (
        <p style={{ color: "#b00020" }}>Error: {error}</p>
      ) : null}

      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button
          type="button"
          onClick={startWatching}
          disabled={starting || !check?.exists || needCloudAck}
        >
          {starting
            ? mode === "change"
              ? "Changing…"
              : "Starting…"
            : mode === "change"
              ? "Change folder"
              : "Start watching"}
        </button>
        <button type="button" onClick={onCancel} disabled={starting}>
          Cancel
        </button>
      </div>
    </div>
  );
}
