// F141 WS-H — Council rooms Quick Start guide overlay.
//
// STATIC content only: takes no room/health data and issues NO fetches, so it
// renders in browser-dev / offline / against remote AIAR. It opens over the room
// editor and never mutates a room. Accessibility (focus trap + Esc + focus
// restore + backdrop close) mirrors F134's QuickStartGuide.
import { useEffect, useRef } from "react";

import {
  COUNCIL_QUICK_START_SECTIONS,
  COUNCIL_QUICK_START_TOC,
} from "./councilQuickStartContent";
import "./CouncilQuickStartGuide.css";

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function CouncilQuickStartGuide({ open, onClose }: Props) {
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
        } else if (active === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open]);

  if (!open) return null;

  const scrollToSection = (id: string) => {
    const target = bodyRef.current?.querySelector<HTMLElement>(
      `#cqs-section-${id}`,
    );
    if (target?.scrollIntoView) {
      target.scrollIntoView({ block: "start" });
    }
  };

  return (
    <div
      ref={dialogRef}
      className="cqs-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="cqs-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="cqs-panel">
        <header className="cqs-head">
          <div>
            <span className="cqs-eyebrow">Council rooms</span>
            <h2 id="cqs-title">Quick Start</h2>
            <p className="cqs-subtitle">
              What each starting point does, and what every room mode means.
            </p>
          </div>
          <button
            type="button"
            className="cqs-close"
            onClick={onClose}
            aria-label="Close Quick Start"
          >
            Close
          </button>
        </header>

        <nav className="cqs-toc" aria-label="Quick Start contents">
          <ol>
            {COUNCIL_QUICK_START_TOC.map((entry) => (
              <li key={entry.id}>
                <button
                  type="button"
                  className="cqs-toc-link"
                  onClick={() => scrollToSection(entry.id)}
                >
                  {entry.title}
                </button>
              </li>
            ))}
          </ol>
        </nav>

        <div className="cqs-body" ref={bodyRef}>
          {COUNCIL_QUICK_START_SECTIONS.map((section) => (
            <section
              key={section.id}
              id={`cqs-section-${section.id}`}
              className="cqs-section"
              aria-labelledby={`cqs-heading-${section.id}`}
            >
              <h3 id={`cqs-heading-${section.id}`}>{section.title}</h3>
              {section.body}
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
