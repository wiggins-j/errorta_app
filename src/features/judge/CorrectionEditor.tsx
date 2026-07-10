import { useEffect, useRef, useState } from "react";
import {
  acceptVerdict,
  draftCorrection,
  type Verdict,
} from "../../lib/api/judge";
import { useToast } from "./toast";

interface Props {
  verdictId: string;
  answer: string;
  verdict: Verdict;
  onAccepted?: (correction: string) => void;
  onCancel?: () => void;
}

export default function CorrectionEditor({
  verdictId,
  answer,
  verdict,
  onAccepted,
  onCancel,
}: Props) {
  const [draft, setDraft] = useState<string>("");
  const [loadingDraft, setLoadingDraft] = useState<boolean>(true);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [acceptedAt, setAcceptedAt] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();
  const toastRef = useRef(toast);
  toastRef.current = toast;
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;

  // Capture opener focus on mount; restore on unmount.
  useEffect(() => {
    previouslyFocusedRef.current =
      (typeof document !== "undefined"
        ? (document.activeElement as HTMLElement | null)
        : null) ?? null;
    // Focus the textarea after first paint.
    const t = setTimeout(() => {
      textareaRef.current?.focus();
    }, 0);
    return () => {
      clearTimeout(t);
      const opener = previouslyFocusedRef.current;
      if (opener && typeof opener.focus === "function") {
        try {
          opener.focus();
        } catch {
          // ignore
        }
      }
    };
  }, []);

  // Escape cancels. Focus trap: Tab/Shift+Tab cycle within the dialog.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancelRef.current?.();
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
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoadingDraft(true);
    setAcceptedAt(null);
    setError(null);
    draftCorrection(answer, verdict)
      .then((r) => {
        if (!cancelled) setDraft(r.draft);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setDraft(answer);
          const msg = e instanceof Error ? e.message : String(e);
          setError(msg);
          toastRef.current.show({
            message: "Couldn't draft correction.",
            details: msg,
          });
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingDraft(false);
      });
    return () => {
      cancelled = true;
    };
  }, [verdictId, answer, verdict]);

  const onAccept = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const r = await acceptVerdict(verdictId, draft);
      setAcceptedAt(r.created_at ?? new Date().toISOString());
      onAccepted?.(draft);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      toastRef.current.show({
        message: "Couldn't save correction.",
        details: msg,
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      ref={dialogRef}
      className="correction-editor"
      role="dialog"
      aria-modal="true"
      aria-labelledby="correction-editor-title"
    >
      <label htmlFor="correction-text" id="correction-editor-title">
        <strong>Proposed correction</strong> — edit, then Accept to persist.
      </label>
      <textarea
        id="correction-text"
        ref={textareaRef}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        disabled={loadingDraft || submitting}
        aria-disabled={loadingDraft || submitting}
        aria-label="Proposed correction text"
        placeholder={loadingDraft ? "Drafting correction…" : ""}
      />
      <div className="actions">
        <button
          type="button"
          className="accept"
          disabled={submitting || loadingDraft || draft.trim().length === 0}
          aria-disabled={submitting || loadingDraft || draft.trim().length === 0}
          onClick={onAccept}
        >
          {submitting ? "Saving…" : "Accept correction"}
        </button>
        {onCancel && (
          <button type="button" className="cancel" onClick={() => onCancel()}>
            Cancel
          </button>
        )}
      </div>
      <div
        className="correction-status"
        role="status"
        aria-live="polite"
        data-testid="correction-status"
      >
        {submitting && <span>Saving…</span>}
        {!submitting && acceptedAt && (
          <span className="accepted">
            Saved. Future runs of this prompt will see it.
          </span>
        )}
        {!submitting && error && <span className="error">{error}</span>}
      </div>
    </div>
  );
}
