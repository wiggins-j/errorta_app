// F008-HISTORY — per-brief edit history dropdown.
//
// Renders a "History" button next to Validate / Run. Clicking it opens a
// dropdown listing up to 5 most recent snapshot timestamps with a humanized
// label and the byte size. Clicking a row opens a read-only modal showing the
// snapshot markdown in a disabled textarea, with a Close button and a
// "Restore" button.
//
// F008-DIFF — adds an opt-in compare mode that surfaces checkboxes on each
// row, lifts the visible-row cap, and allows the user to pick exactly two
// snapshots to render side-by-side via <BriefHistoryDiffView />. Selection is
// FIFO-capped at 2 (picking a 3rd replaces the oldest). The diff view now also
// supports Restore Left / Restore Right (BRIEF-DIFF-KBD): both buttons reuse
// the same restoreBriefSnapshot path as the single-snapshot modal, gated by
// window.confirm. Keyboard nav (ArrowUp/Down/Esc) lets the user swap the left
// snapshot to its older/newer neighbor or close the diff entirely.
import { type ReactNode, useEffect, useState } from "react";
import {
  type BriefHistoryEntry,
  getBriefHistorySnapshot,
  listBriefHistory,
  restoreBriefSnapshot,
} from "../../lib/api/briefs";
import BriefHistoryDiffView from "./BriefHistoryDiffView";

interface Props {
  briefId: string;
  /**
   * Called after a successful restore with the markdown body the brief was
   * restored to. The parent (BriefEditor) uses this to sync its textarea and
   * clear stale validation state.
   */
  onRestore?: (markdown: string) => void;
}

const MAX_VISIBLE_SNAPSHOTS = 5;

function humanizeTimestamp(timestamp: string): string {
  const m = timestamp.match(
    /^(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})(\d{2})(\.\d+)?Z$/,
  );
  if (!m) return timestamp;
  const iso = `${m[1]}T${m[2]}:${m[3]}:${m[4]}${m[5] ?? ""}Z`;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return timestamp;
  return d.toLocaleString();
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Local modal shell so the single-snapshot view and the diff view mount into
// the same dialog markup (same aria-modal semantics, same overlay chrome).
function HistoryModal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="briefs-history-modal"
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 100,
      }}
    >
      <div
        style={{
          background: "var(--bg, #fff)",
          padding: 16,
          minWidth: 480,
          maxWidth: "80vw",
          maxHeight: "80vh",
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}
      >
        <h3>{title}</h3>
        {children}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

type DiffState = {
  leftTs: string;
  rightTs: string;
  leftBody: string;
  rightBody: string;
};

export default function BriefHistoryDropdown({ briefId, onRestore }: Props) {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<BriefHistoryEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState<BriefHistoryEntry | null>(null);
  const [activeBody, setActiveBody] = useState<string>("");
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);

  // F008-DIFF state.
  const [compareMode, setCompareMode] = useState(false);
  // Ordered list (oldest selection first) so we can FIFO-evict at cap=2.
  const [selectedForCompare, setSelectedForCompare] = useState<string[]>([]);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);
  const [diffState, setDiffState] = useState<DiffState | null>(null);
  // BRIEF-DIFF-KBD: which side of the diff is mid-restore (drives button label
  // + disabled state). null = idle.
  const [diffRestoring, setDiffRestoring] = useState<"left" | "right" | null>(
    null,
  );

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    listBriefHistory(briefId)
      .then((list) => {
        if (cancelled) return;
        setEntries(list);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, briefId]);

  const visible = compareMode
    ? (entries ?? [])
    : (entries ?? []).slice(0, MAX_VISIBLE_SNAPSHOTS);

  const openSnapshot = async (entry: BriefHistoryEntry) => {
    setActive(entry);
    setActiveBody("");
    setSnapshotError(null);
    setSnapshotLoading(true);
    try {
      const body = await getBriefHistorySnapshot(briefId, entry.timestamp);
      setActiveBody(body);
    } catch (e) {
      setSnapshotError(e instanceof Error ? e.message : String(e));
    } finally {
      setSnapshotLoading(false);
    }
  };

  const closeModal = () => {
    setActive(null);
    setActiveBody("");
    setSnapshotError(null);
    setRestoreError(null);
    setDiffState(null);
    setCompareError(null);
  };

  const handleRestore = async () => {
    if (!active) return;
    const ok = window.confirm(
      "Restore this snapshot? Your current draft will be saved to history first.",
    );
    if (!ok) return;
    setRestoring(true);
    setRestoreError(null);
    try {
      await restoreBriefSnapshot(briefId, active.timestamp);
      if (onRestore) onRestore(activeBody);
      setActive(null);
      setActiveBody("");
      setSnapshotError(null);
      setOpen(false);
    } catch (e) {
      setRestoreError(e instanceof Error ? e.message : String(e));
    } finally {
      setRestoring(false);
    }
  };

  const toggleCompareMode = () => {
    setCompareMode((v) => {
      const next = !v;
      // Leaving compare mode clears any pending selection so we don't surprise
      // the user the next time they toggle back on.
      if (!next) {
        setSelectedForCompare([]);
        setCompareError(null);
      }
      return next;
    });
  };

  const toggleSelected = (ts: string) => {
    setSelectedForCompare((prev) => {
      if (prev.includes(ts)) {
        // Deselect.
        return prev.filter((t) => t !== ts);
      }
      // FIFO cap at 2: if we already have 2, drop the oldest (first) entry.
      if (prev.length >= 2) {
        return [prev[1], ts];
      }
      return [...prev, ts];
    });
  };

  const handleCompareSelected = async () => {
    if (selectedForCompare.length !== 2) return;
    const [ts1, ts2] = selectedForCompare;
    setCompareLoading(true);
    setCompareError(null);
    try {
      const [leftBody, rightBody] = await Promise.all([
        getBriefHistorySnapshot(briefId, ts1),
        getBriefHistorySnapshot(briefId, ts2),
      ]);
      setDiffState({
        leftTs: ts1,
        rightTs: ts2,
        leftBody,
        rightBody,
      });
    } catch (e) {
      setCompareError(e instanceof Error ? e.message : String(e));
    } finally {
      setCompareLoading(false);
    }
  };

  // BRIEF-DIFF-KBD: compute swap availability. `entries` is sorted descending
  // (newest first). Per spec wording, ArrowUp ("prev") moves to the OLDER
  // neighbor — which is at index+1 in a descending-sorted list. ArrowDown
  // ("next") moves to the NEWER neighbor at index-1.
  const leftTsIdx =
    diffState && entries
      ? entries.findIndex((e) => e.timestamp === diffState.leftTs)
      : -1;
  const canSwapLeftPrev =
    diffState !== null &&
    entries !== null &&
    leftTsIdx >= 0 &&
    leftTsIdx + 1 < entries.length &&
    entries[leftTsIdx + 1].timestamp !== diffState.rightTs;
  const canSwapLeftNext =
    diffState !== null &&
    entries !== null &&
    leftTsIdx > 0 &&
    entries[leftTsIdx - 1].timestamp !== diffState.rightTs;

  const swapLeftTo = async (newTs: string) => {
    if (!diffState) return;
    try {
      const body = await getBriefHistorySnapshot(briefId, newTs);
      setDiffState((prev) =>
        prev ? { ...prev, leftTs: newTs, leftBody: body } : prev,
      );
    } catch (e) {
      setCompareError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleSwapLeftPrev = () => {
    if (!diffState || !entries || leftTsIdx < 0) return;
    const neighbor = entries[leftTsIdx + 1];
    if (!neighbor) return;
    void swapLeftTo(neighbor.timestamp);
  };

  const handleSwapLeftNext = () => {
    if (!diffState || !entries || leftTsIdx <= 0) return;
    const neighbor = entries[leftTsIdx - 1];
    if (!neighbor) return;
    void swapLeftTo(neighbor.timestamp);
  };

  const handleRestoreDiffSide = async (side: "left" | "right") => {
    if (!diffState) return;
    const ts = side === "left" ? diffState.leftTs : diffState.rightTs;
    const body = side === "left" ? diffState.leftBody : diffState.rightBody;
    const ok = window.confirm(
      "Restore this snapshot? Your current draft will be saved to history first.",
    );
    if (!ok) return;
    setDiffRestoring(side);
    setCompareError(null);
    try {
      await restoreBriefSnapshot(briefId, ts);
      if (onRestore) onRestore(body);
      setDiffState(null);
      setOpen(false);
    } catch (e) {
      setCompareError(e instanceof Error ? e.message : String(e));
    } finally {
      setDiffRestoring(null);
    }
  };

  const menuRole = compareMode ? "group" : "menu";

  return (
    <div
      className="briefs-history-dropdown"
      style={{ position: "relative", display: "inline-block" }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        History
      </button>
      {open && (
        <div
          role={menuRole}
          aria-label="Brief history"
          className="briefs-history-menu"
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            zIndex: 10,
            minWidth: 240,
            background: "var(--bg, #fff)",
            border: "1px solid #ccc",
            padding: 4,
          }}
        >
          <div className="briefs-history-toolbar">
            <button
              type="button"
              onClick={toggleCompareMode}
              aria-pressed={compareMode}
            >
              {compareMode ? "Exit compare" : "Compare snapshots"}
            </button>
          </div>
          {loading && <div className="briefs-list-item-meta">Loading…</div>}
          {error && (
            <div role="alert" className="briefs-list-item-meta">
              {error}
            </div>
          )}
          {!loading && !error && visible.length === 0 && (
            <div className="briefs-list-item-meta">No history yet.</div>
          )}
          {!loading && !error && visible.length > 0 && (
            <ul
              className={
                compareMode
                  ? "briefs-history-menu-scroll briefs-history-rows"
                  : "briefs-history-rows"
              }
              style={{ listStyle: "none", margin: 0, padding: 0 }}
            >
              {visible.map((entry) =>
                compareMode ? (
                  <li
                    key={entry.timestamp}
                    className="briefs-history-row"
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                      padding: "4px 8px",
                    }}
                  >
                    <input
                      type="checkbox"
                      aria-label={`Select snapshot ${entry.timestamp}`}
                      checked={selectedForCompare.includes(entry.timestamp)}
                      onChange={() => toggleSelected(entry.timestamp)}
                    />
                    <span>{humanizeTimestamp(entry.timestamp)}</span>
                    <span
                      className="briefs-list-item-meta"
                      style={{ marginLeft: 8 }}
                    >
                      {formatBytes(entry.byte_size)}
                    </span>
                  </li>
                ) : (
                  <li key={entry.timestamp} style={{ listStyle: "none" }}>
                    <button
                      type="button"
                      role="menuitem"
                      className="briefs-history-row"
                      onClick={() => openSnapshot(entry)}
                      style={{
                        display: "block",
                        width: "100%",
                        textAlign: "left",
                        padding: "4px 8px",
                        background: "transparent",
                        border: "none",
                        cursor: "pointer",
                      }}
                    >
                      <span>{humanizeTimestamp(entry.timestamp)}</span>
                      <span
                        className="briefs-list-item-meta"
                        style={{ marginLeft: 8 }}
                      >
                        {formatBytes(entry.byte_size)}
                      </span>
                    </button>
                  </li>
                ),
              )}
            </ul>
          )}
          {compareMode && compareError && (
            <div role="alert" className="briefs-parse-banner">
              {compareError}
            </div>
          )}
          {compareMode && selectedForCompare.length === 2 && (
            <div style={{ padding: 4 }}>
              <button
                type="button"
                onClick={handleCompareSelected}
                disabled={compareLoading}
              >
                {compareLoading ? "Loading…" : "Compare selected"}
              </button>
            </div>
          )}
        </div>
      )}
      {active && !diffState && (
        <HistoryModal
          title={`Snapshot — ${humanizeTimestamp(active.timestamp)}`}
          onClose={closeModal}
        >
          {snapshotLoading && (
            <div className="briefs-list-item-meta">Loading snapshot…</div>
          )}
          {snapshotError && (
            <div role="alert" className="briefs-parse-banner">
              {snapshotError}
            </div>
          )}
          {restoreError && (
            <div role="alert" className="briefs-parse-banner">
              {restoreError}
            </div>
          )}
          <textarea
            value={activeBody}
            disabled
            readOnly
            aria-label="Snapshot markdown"
            style={{ flex: 1, minHeight: 240, fontFamily: "monospace" }}
          />
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button
              type="button"
              onClick={handleRestore}
              disabled={restoring || snapshotLoading || !activeBody}
              aria-label="Restore snapshot"
            >
              {restoring ? "Restoring…" : "Restore"}
            </button>
          </div>
        </HistoryModal>
      )}
      {diffState && (
        <HistoryModal title="Compare snapshots" onClose={closeModal}>
          <BriefHistoryDiffView
            leftMarkdown={diffState.leftBody}
            rightMarkdown={diffState.rightBody}
            leftTimestamp={diffState.leftTs}
            rightTimestamp={diffState.rightTs}
            onClose={closeModal}
            canSwapLeftPrev={canSwapLeftPrev}
            canSwapLeftNext={canSwapLeftNext}
            onSwapLeftPrev={handleSwapLeftPrev}
            onSwapLeftNext={handleSwapLeftNext}
            onRestoreLeft={() => handleRestoreDiffSide("left")}
            onRestoreRight={() => handleRestoreDiffSide("right")}
            restoring={diffRestoring}
          />
        </HistoryModal>
      )}
    </div>
  );
}
