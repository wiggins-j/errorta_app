// F135 — "What the PM has learned" info sheet.
//
// A read-only modal, launched from inside the PM Governance panel, that renders
// the GLOBAL, cross-project PM learning digest: per route × task-kind, how the
// model has actually done, with a four-way standing (preferred / cautioned /
// demoted / learning). The demotion threshold shown here is the SAME one the
// selector acts on, so the explanation can't drift from the behavior.
//
// Modal a11y (role=dialog + aria-modal, Esc + backdrop close, Tab focus-trap,
// opener-focus restore) is copied from src/features/corpus/RefreshDiffModal.tsx
// — there is no shared modal utility in the repo.
import { useEffect, useRef, useState } from "react";
import { getModelLearning } from "../../lib/api/coding";
import type { ModelLearningDigest, ModelStanding } from "../../lib/api/coding";

const STANDING_LABEL: Record<ModelStanding, string> = {
  preferred: "Preferred",
  cautioned: "Cautioned",
  demoted: "Demoted",
  insufficient_data: "Learning",
};

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

function bucketLine(
  standing: ModelStanding,
  taskType: string,
  difficulty: string,
  accepted: number,
  attempts: number,
): string {
  const where = `${difficulty} ${taskType}`.trim();
  if (standing === "insufficient_data") {
    return `Still learning on ${where} — ${accepted}/${attempts} accepted so far (needs more).`;
  }
  if (standing === "demoted") {
    return `Demoted for ${where} — accepted ${accepted}/${attempts}; the PM now prefers a stronger model here.`;
  }
  if (standing === "cautioned") {
    return `Mixed on ${where} — accepted ${accepted}/${attempts}.`;
  }
  return `Preferred for ${where} — accepted ${accepted}/${attempts}.`;
}

export default function PmLearningSheet({ isOpen, onClose }: Props) {
  const [digest, setDigest] = useState<ModelLearningDigest | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Fetch the (global, project-agnostic) digest each time the sheet opens.
  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getModelLearning()
      .then((d) => {
        if (!cancelled) setDigest(d);
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load PM learning");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen]);

  // Capture opener focus on open; restore on close/unmount. Focus the close
  // button on open so keyboard users land inside the dialog.
  useEffect(() => {
    if (!isOpen) return;
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

  const summary = digest?.summary;
  const routes = digest?.routes ?? [];
  const hasLearning = Boolean(summary?.corpusAvailable) && routes.length > 0;

  return (
    <div
      ref={dialogRef}
      className="coding-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="pm-learning-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="coding-modal coding-pm-learning-sheet">
        <div className="coding-modal-head">
          <h3 id="pm-learning-title">What the PM has learned</h3>
          <button
            ref={closeButtonRef}
            type="button"
            className="coding-btn coding-btn-small"
            onClick={onClose}
          >
            Close
          </button>
        </div>

        <p className="coding-field-hint">
          The PM&apos;s model knowledge is <strong>shared across all your
          projects</strong> — a new project starts from everything learned so far.
          {summary
            ? ` ${summary.totalAttempts} task attempt${
                summary.totalAttempts === 1 ? "" : "s"
              } over the last ${summary.windowDays} days.`
            : ""}
        </p>

        {loading && (
          <div className="coding-pm-learning-loading" aria-live="polite">
            Loading…
          </div>
        )}

        {error && (
          <div className="coding-parse-banner" role="alert">
            {error}
          </div>
        )}

        {!loading && !error && !hasLearning && (
          <p className="coding-empty">
            No model performance recorded yet — the PM will start learning on the
            first completed task.
          </p>
        )}

        {!loading && !error && hasLearning && (
          <ul className="coding-pm-learning-routes" aria-label="Model standings">
            {routes.map((route) => (
              <li key={route.routeId} className="coding-pm-learning-route">
                <div className="coding-pm-learning-route-head">
                  <strong>{route.routeId}</strong>
                  <span className="coding-cap">
                    {route.capabilityTier} · cost {route.costTier}
                    {route.tiersUnset ? " · tiers unset" : ""}
                  </span>
                </div>
                <ul className="coding-pm-learning-buckets">
                  {route.buckets.map((b) => (
                    <li
                      key={`${b.taskType}:${b.difficultyTier}`}
                      className={`coding-standing coding-standing-${b.standing}`}
                    >
                      <span className="coding-standing-badge">
                        {STANDING_LABEL[b.standing]}
                      </span>
                      <span className="coding-standing-line">
                        {bucketLine(
                          b.standing,
                          b.taskType,
                          b.difficultyTier,
                          b.accepted,
                          b.attempts,
                        )}
                      </span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
