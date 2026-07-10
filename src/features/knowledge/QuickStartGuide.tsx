// F134 — Knowledge Quick Start guide overlay.
//
// STATIC content only: this component takes no catalog/watch/brief/health data
// and issues NO fetches, so it renders in browser-dev / offline / against remote
// AIAR. It opens over the current panel and never changes the active feature.
// Accessibility (focus trap + Esc + focus restore + backdrop close) reuses the
// applied pattern from src/features/corpus/RefreshDiffModal.tsx.
import { useEffect, useRef } from "react";

import { QUICK_START_SECTIONS, QUICK_START_TOC } from "./quickStartContent";
import "./knowledge.css";

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function QuickStartGuide({ open, onClose }: Props) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Capture opener focus on open; restore when closing/unmounting.
  useEffect(() => {
    if (!open) return;
    previouslyFocusedRef.current =
      (typeof document !== "undefined"
        ? (document.activeElement as HTMLElement | null)
        : null) ?? null;
    // Move focus into the dialog so keyboard users land inside it.
    dialogRef.current?.querySelector<HTMLElement>("button, [href]")?.focus();
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
  }, [open]);

  // Escape closes. Tab/Shift+Tab cycle focus within the dialog.
  useEffect(() => {
    if (!open) return;
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
  }, [open]);

  if (!open) return null;

  const scrollToSection = (id: string) => {
    const target = bodyRef.current?.querySelector<HTMLElement>(
      `#quickstart-section-${id}`,
    );
    if (target?.scrollIntoView) {
      target.scrollIntoView({ block: "start" });
    }
  };

  return (
    <div
      ref={dialogRef}
      className="quickstart-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="quickstart-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="quickstart-panel">
        <header className="quickstart-head">
          <div>
            <span className="knowledge-eyebrow">Knowledge</span>
            <h2 id="quickstart-title">Quick Start</h2>
            <p className="quickstart-subtitle">
              Build a corpus, then ask questions against it. Two minutes,
              start to finish.
            </p>
          </div>
          <button
            type="button"
            className="quickstart-close"
            onClick={onClose}
            aria-label="Close Quick Start"
          >
            Close
          </button>
        </header>

        <nav className="quickstart-toc" aria-label="Quick Start contents">
          <ol>
            {QUICK_START_TOC.map((entry) => (
              <li key={entry.id}>
                <button
                  type="button"
                  className="quickstart-toc-link"
                  onClick={() => scrollToSection(entry.id)}
                >
                  {entry.title}
                </button>
              </li>
            ))}
          </ol>
        </nav>

        <div className="quickstart-body" ref={bodyRef}>
          {QUICK_START_SECTIONS.map((section) => (
            <section
              key={section.id}
              id={`quickstart-section-${section.id}`}
              className="quickstart-section"
              aria-labelledby={`quickstart-heading-${section.id}`}
            >
              <h3 id={`quickstart-heading-${section.id}`}>{section.title}</h3>
              {section.body}
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
