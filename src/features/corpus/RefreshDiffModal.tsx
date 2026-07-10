// F015-UI — corpus refresh preview modal.
//
// Preview-only: the modal shows what *would* change if the corpus were
// refreshed against its on-disk state. There is no apply path here per the
// sidecar's refresh.py scope comment — the user inspects the diff and
// closes. Re-uses the .briefs-modal-backdrop / .briefs-modal / .briefs-parse-banner
// class names to keep the visual language consistent without introducing
// new CSS in this slice.
import { useEffect, useRef, useState } from "react";
import { refreshApply } from "../../lib/api/corpus";
import type { ApplyResult, RefreshDiffResponse } from "./types";

type TabKey = "added" | "removed" | "updated";

interface Props {
  isOpen: boolean;
  onClose: () => void;
  corpus: string;
  diff: RefreshDiffResponse | null;
  loading: boolean;
  error: string | null;
  /** Called with the ApplyResult after a successful refresh-apply. */
  onApplied?: (result: ApplyResult) => void;
}

export default function RefreshDiffModal({
  isOpen,
  onClose,
  corpus,
  diff,
  loading,
  error,
  onApplied,
}: Props) {
  const [activeTab, setActiveTab] = useState<TabKey>("added");
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applySuccess, setApplySuccess] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Capture opener focus on open; restore when closing/unmounting.
  useEffect(() => {
    if (!isOpen) return;
    previouslyFocusedRef.current =
      (typeof document !== "undefined"
        ? (document.activeElement as HTMLElement | null)
        : null) ?? null;
    return () => {
      const opener = previouslyFocusedRef.current;
      if (opener && typeof opener.focus === "function") {
        try {
          opener.focus();
        } catch {
          // ignore
        }
      }
    };
  }, [isOpen]);

  // Escape closes. Tab/Shift+Tab cycle focus within the dialog.
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onCloseRef.current?.();
        return;
      }
      if (e.key === "Tab" && dialogRef.current) {
        const focusables = dialogRef.current.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        );
        const enabled = Array.from(focusables).filter(
          (el) => !el.hasAttribute("disabled"),
        );
        if (enabled.length === 0) return;
        const first = enabled[0];
        const last = enabled[enabled.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey) {
          if (active === first || !dialogRef.current.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (active === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [isOpen]);

  const handleApply = async () => {
    if (!diff) return;
    setApplying(true);
    setApplyError(null);
    setApplySuccess(null);
    try {
      const result = await refreshApply(corpus, diff);
      const count =
        result.ingested.length + result.removed.length + result.updated.length;
      const errPart = result.errors.length
        ? ` (${result.errors.length} error${result.errors.length === 1 ? "" : "s"})`
        : "";
      setApplySuccess(`Applied ${count} change${count === 1 ? "" : "s"}${errPart}.`);
      onApplied?.(result);
      onClose();
    } catch (e) {
      setApplyError(
        e instanceof Error ? e.message : "Failed to apply refresh diff",
      );
    } finally {
      setApplying(false);
    }
  };

  if (!isOpen) return null;

  const added = diff?.added ?? [];
  const removed = diff?.removed ?? [];
  const updated = diff?.updated ?? [];
  const totalChanges = added.length + removed.length + updated.length;
  const hasDiff = diff !== null;

  const renderTabPanel = () => {
    if (activeTab === "added") {
      return (
        <ul className="refresh-diff-list">
          {added.map((entry, i) => (
            <li key={`a-${i}-${entry.original_path}`}>{entry.original_path}</li>
          ))}
        </ul>
      );
    }
    if (activeTab === "removed") {
      return (
        <ul className="refresh-diff-list">
          {removed.map((entry, i) => (
            <li key={`r-${i}-${entry.original_path}`}>{entry.original_path}</li>
          ))}
        </ul>
      );
    }
    return (
      <ul className="refresh-diff-list">
        {updated.map((entry, i) => (
          <li key={`u-${i}-${entry.old.original_path}`}>
            <span>{entry.old.original_path}</span>
            <span aria-hidden="true"> → </span>
            <span>{entry.new.original_path}</span>
          </li>
        ))}
      </ul>
    );
  };

  return (
    <div
      ref={dialogRef}
      className="briefs-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="refresh-diff-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="briefs-modal">
        <h3 id="refresh-diff-title">Preview changes for {corpus}</h3>

        {error && (
          <div className="briefs-parse-banner" role="alert">
            {error}
          </div>
        )}

        {loading && !error && (
          <div className="refresh-diff-loading" aria-live="polite">
            Loading preview…
          </div>
        )}

        {!loading && !error && hasDiff && totalChanges === 0 && (
          <div className="refresh-diff-empty">No changes</div>
        )}

        {!loading && !error && hasDiff && totalChanges > 0 && (
          <>
            <div role="tablist" aria-label="Diff sections" className="refresh-diff-tabs">
              <button
                type="button"
                role="tab"
                aria-selected={activeTab === "added"}
                className={activeTab === "added" ? "primary" : ""}
                onClick={() => setActiveTab("added")}
              >
                Added ({added.length})
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={activeTab === "removed"}
                className={activeTab === "removed" ? "primary" : ""}
                onClick={() => setActiveTab("removed")}
              >
                Removed ({removed.length})
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={activeTab === "updated"}
                className={activeTab === "updated" ? "primary" : ""}
                onClick={() => setActiveTab("updated")}
              >
                Updated ({updated.length})
              </button>
            </div>
            <div role="tabpanel" className="refresh-diff-panel">
              {renderTabPanel()}
            </div>
          </>
        )}

        {hasDiff && diff && (
          <div className="refresh-diff-footer">
            Snapshot taken at {new Date(diff.snapshot_at).toLocaleString()}
          </div>
        )}

        {applyError && (
          <div className="briefs-parse-banner" role="alert">
            {applyError}
            <button
              type="button"
              className="refresh-diff-retry"
              onClick={() => {
                void handleApply();
              }}
            >
              Retry
            </button>
          </div>
        )}

        {applySuccess && (
          <div className="refresh-diff-success" role="status" aria-live="polite">
            {applySuccess}
          </div>
        )}

        <div className="briefs-modal-actions">
          <button type="button" onClick={onClose} disabled={applying}>
            Close
          </button>
          <button
            type="button"
            className="primary"
            disabled={!hasDiff || totalChanges === 0 || applying || loading}
            onClick={() => {
              void handleApply();
            }}
          >
            {applying ? (
              <>
                <span
                  className="refresh-diff-spinner"
                  role="status"
                  aria-label="Applying"
                />
                Applying…
              </>
            ) : (
              "Apply"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
