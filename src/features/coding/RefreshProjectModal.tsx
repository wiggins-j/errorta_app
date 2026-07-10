// F138 — Refresh an imported project from remote.
//
// A confirm modal launched from the OnboardingPanel's staleness badge. Shows what
// the refresh would do (remote ahead / repo differs / un-accepted snapshot work),
// then runs the guarded job: (optionally) fast-forward the repo to its remote
// default branch, then re-seed the Coding Team's snapshot. Refusals from the
// backend map to actionable copy; a snapshot holding un-accepted work requires an
// explicit Discard. Modal a11y (Esc + backdrop + focus-trap + restore) mirrors
// PmLearningSheet / RefreshDiffModal — no shared modal utility exists.
import { useEffect, useRef, useState } from "react";

import { refreshProject, refreshProjectStatus } from "../../lib/api/coding";
import type { RefreshPreview } from "../../lib/api/coding";

interface Props {
  isOpen: boolean;
  projectId: string;
  preview: RefreshPreview | null;
  onClose: () => void;
  onRefreshed: () => void;
}

// Backend refusal reason codes -> actionable copy.
const REASON_COPY: Record<string, string> = {
  repo_dirty:
    "Your repo has uncommitted changes. Commit, stash, or publish them first, then refresh.",
  repo_detached:
    "Your repo is on a detached HEAD. Check out the default branch first, then refresh.",
  repo_rebase_in_progress:
    "Your repo is mid-rebase or merge. Finish that first, then refresh.",
  not_on_default_branch:
    "Your repo isn't on its default branch, so it can't be pulled. Switch to the default branch, or refresh without pulling to re-seed from the current branch.",
  branch_diverged:
    "Your branch has diverged from the remote (it has commits the remote doesn't). Reconcile it in your own git tools, then refresh.",
  unaccepted_changes:
    "Errorta's workspace has changes from the last run that aren't merged back. Publish/accept them, or check Discard to re-seed anyway.",
  repo_path_missing: "The imported folder no longer exists on disk.",
  run_active: "A Coding Team run started before refresh could begin. Stop it and try again.",
  refresh_failed: "The refresh couldn't complete. Check the repo and try again.",
};

function reasonCopy(code: string | null): string {
  if (!code) return "The refresh couldn't complete.";
  return REASON_COPY[code] ?? code;
}

export default function RefreshProjectModal({
  isOpen,
  projectId,
  preview,
  onClose,
  onRefreshed,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [discard, setDiscard] = useState(false);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const mounted = useRef(true);
  const activeRunRef = useRef(0);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!isOpen) {
      // The backend job may continue, but a dismissed modal must not later fire
      // stale callbacks or update state when its poll completes.
      activeRunRef.current += 1;
      return;
    }
    setError(null);
    setDiscard(false);
    setBusy(false);
    previouslyFocusedRef.current =
      (typeof document !== "undefined"
        ? (document.activeElement as HTMLElement | null)
        : null) ?? null;
    closeButtonRef.current?.focus();
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
        } else if (active === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [isOpen]);

  if (!isOpen) return null;

  const pv = preview;
  const previewReady = pv !== null;
  const originPresent = pv?.originPresent ?? false;
  const remoteAhead = pv?.remoteAhead ?? null;
  const needsDiscard = pv?.workspaceHasUnacceptedChanges ?? false;
  const actionLabel = originPresent ? "Pull and re-seed" : "Re-seed from folder";

  const run = async () => {
    const activeRun = ++activeRunRef.current;
    setBusy(true);
    setError(null);
    try {
      const job = await refreshProject(projectId, {
        pull: originPresent,
        discardWorkspace: discard,
      });
      let status = job;
      for (let i = 0; i < 600 && status.status === "refreshing"; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        if (!mounted.current || activeRunRef.current !== activeRun) return;
        status = await refreshProjectStatus(projectId, job.jobId);
      }
      if (!mounted.current || activeRunRef.current !== activeRun) return;
      if (status.status === "done") {
        onRefreshed();
        onClose();
      } else {
        setError(status.message ?? "refresh_failed");
      }
    } catch (err) {
      if (mounted.current && activeRunRef.current === activeRun) {
        setError(err instanceof Error ? err.message : "refresh_failed");
      }
    } finally {
      if (mounted.current && activeRunRef.current === activeRun) setBusy(false);
    }
  };

  return (
    <div
      className="coding-modal-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="coding-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="refresh-project-title"
      >
        <header className="coding-modal-head">
          <h3 id="refresh-project-title">Refresh from remote</h3>
          <button
            ref={closeButtonRef}
            type="button"
            className="coding-btn coding-btn-ghost"
            onClick={onClose}
            aria-label="Close"
          >
            Close
          </button>
        </header>

        <div className="coding-refresh-body">
          {!previewReady ? (
            <p className="coding-refresh-warn" role="status">
              Refresh preview is unavailable. Close this dialog and try again.
            </p>
          ) : originPresent ? (
            <p>
              {remoteAhead && remoteAhead > 0
                ? `The remote is ${remoteAhead} commit${remoteAhead === 1 ? "" : "s"} ahead of Errorta's snapshot`
                : "Errorta's snapshot is up to date with the remote"}
              {pv?.defaultBranch ? ` (origin/${pv.defaultBranch}).` : "."}
            </p>
          ) : (
            <p>
              This project has no GitHub remote. Refresh re-seeds Errorta's snapshot
              from the current folder.
            </p>
          )}

          {pv?.repoDirty ? (
            <p className="coding-refresh-warn" role="status">
              Your repo has uncommitted changes.
              {originPresent
                ? " The remote pull will be refused until it's clean — commit, stash, or publish first."
                : ""}
            </p>
          ) : null}

          {needsDiscard ? (
            <label className="coding-refresh-discard">
              <input
                type="checkbox"
                checked={discard}
                onChange={(e) => setDiscard(e.target.checked)}
              />
              Errorta's workspace has un-accepted changes from the last run.
              Discard them and re-seed anyway.
            </label>
          ) : null}

          {error ? (
            <p className="coding-refresh-error" role="alert">
              {reasonCopy(error)}
            </p>
          ) : null}
        </div>

        <div className="coding-modal-actions">
          <button
            type="button"
            className="coding-btn coding-btn-primary"
            onClick={() => void run()}
            disabled={busy || !previewReady || (needsDiscard && !discard)}
          >
            {busy ? "Refreshing…" : actionLabel}
          </button>
          <button
            type="button"
            className="coding-btn coding-btn-ghost"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
