// F134 — shared open/dismiss state for the Knowledge Quick Start guide.
//
// Each Knowledge panel (Corpus / Briefs / Folder Watcher) mounts the guide and
// a header "Quick Start" control. This hook centralizes the open state and the
// empty-state dismissal flag so all three behave identically without per-panel
// duplication. No network, no app-nav side effects.
import { useCallback, useState } from "react";

export const QUICK_START_DISMISSED_KEY = "errorta.knowledge.quickstart.dismissed";

function readDismissed(): boolean {
  try {
    return localStorage.getItem(QUICK_START_DISMISSED_KEY) === "1";
  } catch {
    return false;
  }
}

export interface QuickStartControl {
  /** Whether the guide overlay is currently open. */
  open: boolean;
  /** Whether the prominent empty-state offer has been dismissed. */
  dismissed: boolean;
  openGuide(): void;
  closeGuide(): void;
  /** Hide the empty-state offer permanently (the header control still opens it). */
  dismiss(): void;
}

export function useQuickStart(): QuickStartControl {
  const [open, setOpen] = useState(false);
  const [dismissed, setDismissed] = useState(readDismissed);

  const openGuide = useCallback(() => setOpen(true), []);
  const closeGuide = useCallback(() => setOpen(false), []);

  const dismiss = useCallback(() => {
    setDismissed(true);
    try {
      localStorage.setItem(QUICK_START_DISMISSED_KEY, "1");
    } catch {
      // Ignore storage failures; the in-memory flag still hides the offer.
    }
  }, []);

  return { open, dismissed, openGuide, closeGuide, dismiss };
}
